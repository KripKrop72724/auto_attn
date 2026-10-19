#include "storage_repair_core.h"

#include <stdio.h>
#include <string.h>

#include "file_transaction.h"
#include "spiffs_nucleus.h"

/* ESP-IDF's SPIFFS layout with 16-bit ids and one lookup page per block, as
 * Zone Lite builds it (page 256, block 4096, data span 251). */
#define SR_IX SPIFFS_OBJ_ID_IX_FLAG
#define SR_NO_PAGE ((uint16_t)0xFFFF)
#define SR_IDS 0x8000U
/* Engine codes, outside SPIFFS's range, for what SPIFFS cannot name. */
#define SR_ERR_ERASED_CONTENT (-10200)   /* a text page holds erased (0xFF) bytes */
#define SR_ERR_LOOKUP_MISMATCH (-10201)  /* the page is right but its lookup entry is not */
#define SR_ERR_SOURCES_DISAGREE (-10202) /* the pages that hold this span disagree on a byte */
#define SR_ROW_WINDOW 8192U              /* longest row a drain reads (DQ_MAX_RECORD_BYTES) */
#define SR_HOLD_CHUNK (64U * 1024U)
#define SR_MAX_OBJECTS 320U
#define SR_MAX_CHECK_PASSES 4U
#define SR_NORMALIZE_ROUNDS 3U
#define SR_YIELD_EVERY 64U
#define SR_COPY_BYTES 4016U
/* What a check pass may take before one has been measured, and what the
 * restore after it may take: the surveys, and rewriting every held byte. */
#define SR_CHECK_MS_PER_MIB_DEFAULT 8000U
#define SR_RESTORE_BASE_MS 15000U
#define SR_RESTORE_BYTES_PER_MS 64U

enum { PATCH_COPY = 1, PATCH_REFER };
enum { SOURCE_FILL = 0, SOURCE_RELOCATE, SOURCE_ALTERNATE, SOURCE_DELETED, SOURCE_PARTIAL, SOURCE_TAIL, SOURCE_HELD };

typedef struct {
    uint16_t lu, obj, span;
    uint8_t flags, erased;
} pg_t;

typedef struct {
    uint16_t pix, obj;
    uint32_t size;
    char name[SRP_NAME_BYTES];
    bool hidden, named;
} hdr_t;

/* The new content of one data span. mask marks the bytes nobody could read
 * (a gap); they hold srp_fill bytes once filled. */
typedef struct {
    uint32_t span;
    uint8_t kind, source, filled;
    uint16_t length, refer, from;
    int32_t code;
    uint8_t mask[(SRP_SPAN_BYTES + 7) / 8];
    uint8_t data[SRP_SPAN_BYTES];
} patch_t;

/* The bytes of one span nobody could read when its file was held. */
typedef struct {
    uint32_t span;
    uint8_t mask[(SRP_SPAN_BYTES + 7) / 8];
} held_gap_t;

typedef struct {
    char name[SRP_NAME_BYTES];
    srp_kind_t kind;
    uint16_t obj, header;
    uint32_t size, spans, ix_spans;
    uint32_t header_size;    /* what the chosen header holds, when the size was recovered */
    bool hidden, plausible, mapped, consistent, needs_work, held, unrecordable, size_recovered, retired;
    uint16_t *ix;            /* chosen index page per index span; SR_NO_PAGE when missing */
    uint8_t *ix_rewrite;     /* per index span: rewrite (or create) it */
    uint16_t *ref;           /* referenced page per data span */
    uint8_t *perfect;        /* per data span */
    patch_t *patch;
    uint32_t patches, patch_cap;
    uint16_t *drop;          /* stale headers, duplicate or stale index pages, stray pages to delete */
    uint32_t drops, drop_cap, strays;
    uint8_t **hold;
    uint32_t hold_chunks, hold_size, crc;
    held_gap_t *held_gaps;   /* by span: what was unreadable when the hold was taken */
    uint32_t held_gap_count;
    uint32_t gaps, gap_bytes, rewritten;
    uint32_t blocked_spans, blocked_offset;
    uint16_t actions;
} obj_t;

typedef struct {
    spiffs *fs;
    const srr_config_t *cfg;
    const srr_ports_t *ports;
    srr_report_t *report;
    uint32_t page_size, block_size, ppb, entries, blocks, total_pages, hdr_ix, ix_len;
    pg_t *pages;
    hdr_t *hdrs;
    uint32_t hdr_count, hdr_cap;
    uint16_t *ixpages;
    uint32_t ixpage_count, ixpage_cap;
    uint16_t *orphans;             /* index pages past span 0 of no live file */
    uint32_t orphan_count, orphan_cap;
    uint16_t *span_top;            /* per object id: 1 + the highest span of its finalized data pages */
    obj_t *objs;
    uint32_t obj_count;
    uint8_t *block, *page;
    uint16_t *cand_valid, *cand_next; /* per-object candidate lists */
    uint64_t hold_used;
    srp_state_t state;
    srp_manifest_t *manifest;      /* the active manifest: loaded for a resume, or this run's */
    srp_gaps_t *gaps;
    srp_resets_t *resets;
    bool stopped, failed, wrote, catalog_damaged, unrecordable_custody;
    bool finishing;                /* the restore after a check pass: only the hard deadline ends it */
    bool was_full;                 /* the previous run deferred for room (SRP_STATE_FULL) */
    uint32_t was_shortage;         /* and what it lacked (srp_state_t.shortage) */
    bool restore_failed;
    uint32_t ops;
    uint32_t last_check_ms;
    /* check callback counters */
    uint32_t fix_index, fix_lookup, del_orphan, del_page, del_bad_file, errors;
    int32_t last_error;
    uint16_t deleted_ids[16];
    uint32_t deleted_count;
} run_t;

static run_t *s_run;

const char *srr_result_name(srr_result_t result)
{
    switch (result) {
    case SRR_RESULT_NOT_RUN: return "NOT_RUN";
    case SRR_RESULT_CLEAN: return "CLEAN";
    case SRR_RESULT_REPAIRED: return "REPAIRED";
    case SRR_RESULT_REPAIRED_WITH_GAPS: return "REPAIRED_WITH_GAPS";
    case SRR_RESULT_DEFERRED: return "DEFERRED";
    case SRR_RESULT_NORMALIZED: return "NORMALIZED";
    case SRR_RESULT_REPORT_ONLY: return "REPORT_ONLY";
    case SRR_RESULT_STOPPED: return "STOPPED";
    case SRR_RESULT_FAILED: return "FAILED";
    case SRR_RESULT_REFORMATTED: return "REFORMATTED";
    }
    return "UNKNOWN";
}

/* ---- small helpers -------------------------------------------------- */
static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}
static void put32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static bool bit(const uint8_t *mask, uint32_t i) { return (mask[i / 8] >> (i % 8)) & 1U; }
static void set_bit(uint8_t *mask, uint32_t i) { mask[i / 8] |= (uint8_t)(1U << (i % 8)); }
static void clear_bit(uint8_t *mask, uint32_t i) { mask[i / 8] &= (uint8_t)~(1U << (i % 8)); }

static void *zalloc(run_t *r, size_t bytes)
{
    void *p = bytes ? r->ports->alloc(r->ports->context, bytes) : NULL;
    if (p) memset(p, 0, bytes);
    return p;
}
static void release(run_t *r, void *p)
{
    if (p) r->ports->release(r->ports->context, p);
}

static void logf_line(run_t *r, const char *level, const char *code, const char *message)
{
    if (r->ports->log) r->ports->log(r->ports->context, level, code, message);
}

/* The soft deadline stops the run at its next safe point. The restore after
 * a check pass is not stopped: the held copies die with this boot, so it
 * finishes unless the hard deadline restarts the ESP. */
static bool going(run_t *r)
{
    if (r->finishing) return true;
    if (!r->stopped && r->ports->keep_going && !r->ports->keep_going(r->ports->context)) r->stopped = true;
    return !r->stopped;
}

static void pace(run_t *r)
{
    if (++r->ops % SR_YIELD_EVERY == 0 && r->ports->yield) r->ports->yield(r->ports->context);
}

static bool fail(run_t *r, const char *stage, int32_t code)
{
    if (!r->failed) {
        r->failed = true;
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "%s", stage);
        r->report->error_code = code;
    }
    return false;
}

static void clear_failure(run_t *r)
{
    r->failed = false;
    r->report->error_stage[0] = '\0';
    r->report->error_code = 0;
}

static void set_phase(run_t *r, srp_phase_t phase, uint32_t pass, bool writing)
{
    r->report->phase = phase;
    if (r->ports->phase) r->ports->phase(r->ports->context, phase, pass, writing);
}

static bool hal_read(run_t *r, uint32_t addr, void *buffer, uint32_t length)
{
    pace(r);
    return r->fs->cfg.hal_read_f(r->fs, addr, length, buffer) == SPIFFS_OK;
}

static bool index_valid(uint8_t flags)
{
    return !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) &&
        (flags & SPIFFS_PH_FLAG_DELET);
}
static bool data_valid(uint8_t flags)
{
    return !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL)) && (flags & SPIFFS_PH_FLAG_DELET) &&
        (flags & SPIFFS_PH_FLAG_INDEX);
}
static bool page_ok(const run_t *r, uint32_t pix)
{
    return pix < r->total_pages && pix % r->ppb != 0;
}

static bool text_fill(const obj_t *o)
{
    return o->kind.fill == SRP_FILL_JSON_ROWS || o->kind.fill == SRP_FILL_ID_LINES;
}

static bool custody(const obj_t *o)
{
    return o->kind.cls == SRP_CLASS_ATTENDANCE || o->kind.cls == SRP_CLASS_EVIDENCE ||
        o->kind.cls == SRP_CLASS_OPERATIONAL || o->kind.cls == SRP_CLASS_DEDUPE || o->kind.cls == SRP_CLASS_UNKNOWN;
}

static bool live(const obj_t *o)
{
    return !o->hidden && o->header != SR_NO_PAGE && o->plausible;
}

static bool restore_name(const char *name, char *out)
{
    int n = snprintf(out, SRP_NAME_BYTES, "%s%s", name, SRP_RESTORE_SUFFIX);
    return n > 0 && (size_t)n < SPIFFS_OBJ_NAME_LEN;
}

static bool restore_file(const char *name)
{
    size_t length = strlen(name), suffix = sizeof(SRP_RESTORE_SUFFIX) - 1U;
    return length > suffix && !strcmp(name + length - suffix, SRP_RESTORE_SUFFIX);
}

/* ---- NVS ------------------------------------------------------------ */
static int nvs_load(run_t *r, const char *ns, const char *key, void *blob, size_t size)
{
    return r->ports->nvs_load ? r->ports->nvs_load(r->ports->context, ns, key, blob, size) : 0;
}
static bool nvs_store(run_t *r, const char *ns, const char *key, const void *blob, size_t size)
{
    return r->ports->nvs_store && r->ports->nvs_store(r->ports->context, ns, key, blob, size);
}
static bool save_state(run_t *r)
{
    srp_state_seal(&r->state);
    return nvs_store(r, SRR_NVS_NAMESPACE, "state", &r->state, sizeof(r->state));
}
static bool save_records(run_t *r)
{
    return nvs_store(r, SRR_NVS_NAMESPACE, "gaps", r->gaps, sizeof(*r->gaps)) &&
        nvs_store(r, SRR_NVS_NAMESPACE, "resets", r->resets, sizeof(*r->resets));
}

/* The generation of a legacy queue file's checkpoint: it changes whenever
 * its owner retires the file (lq_reclaim) or a cursor is cleared. */
static bool cursor_generation(run_t *r, const srp_kind_t *kind, uint32_t *generation)
{
    lq_checkpoint_t cp;
    if (!kind->lq_namespace || nvs_load(r, kind->lq_namespace, kind->lq_key, &cp, sizeof(cp)) != 1 ||
        !lq_checkpoint_valid(&cp))
        return false;
    *generation = cp.generation;
    return true;
}

/* ---- manifest -------------------------------------------------------- */
static bool manifest_active(const run_t *r) { return (r->state.flags & SRP_STATE_MANIFEST) != 0; }

static srp_manifest_file_t *manifest_entry(run_t *r, const char *name)
{
    if (!manifest_active(r)) return NULL;
    for (uint32_t i = 0; i < r->manifest->count && i < SRP_MANIFEST_FILES; ++i)
        if (!strcmp(r->manifest->file[i].name, name)) return &r->manifest->file[i];
    return NULL;
}

static bool manifest_save(run_t *r)
{
    srp_manifest_seal(r->manifest);
    return nvs_store(r, SRR_NVS_NAMESPACE, "manifest", r->manifest, sizeof(*r->manifest));
}

static bool manifest_mark(run_t *r, const char *name, uint8_t flags)
{
    srp_manifest_file_t *f = manifest_entry(r, name);
    if (!f || (f->flags & flags) == flags) return true;
    f->flags |= flags;
    return manifest_save(r) || fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
}

/* Every entry settled: the manifest no longer describes anything at risk. */
static bool manifest_retire(run_t *r)
{
    if (!manifest_active(r)) return true;
    r->state.flags &= ~SRP_STATE_MANIFEST;
    if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    if (r->ports->nvs_erase) (void)r->ports->nvs_erase(r->ports->context, SRR_NVS_NAMESPACE, "manifest");
    return true;
}

/* The unsettled manifest entry whose own copy under the temporary name this
 * object is: the repair's rebuild of a held file, or the original adopted
 * there while a new file has the name (rebuild_id names it before it is
 * written). Such a copy is custody of its original's kind, never expendable,
 * and nothing but the resume writes it. */
static srp_manifest_file_t *rebuild_entry(run_t *r, const char *name, uint16_t obj)
{
    if (!manifest_active(r) || !restore_file(name)) return NULL;
    for (uint32_t i = 0; i < r->manifest->count && i < SRP_MANIFEST_FILES; ++i) {
        srp_manifest_file_t *f = &r->manifest->file[i];
        char temp[SRP_NAME_BYTES];
        if (!(f->flags & SRP_MANIFEST_RESTORING) || (f->flags & SRP_MANIFEST_SETTLED) || !f->rebuild_id ||
            f->rebuild_id != obj)
            continue;
        if (restore_name(f->name, temp) && !strcmp(temp, name)) return f;
    }
    return NULL;
}

/* ---- survey: one pass over every block ------------------------------ */
static void release_maps(run_t *r, obj_t *o)
{
    release(r, o->ix);
    release(r, o->ix_rewrite);
    release(r, o->ref);
    release(r, o->perfect);
    release(r, o->patch);
    release(r, o->drop);
    o->ix = o->ref = o->drop = NULL;
    o->ix_rewrite = o->perfect = NULL;
    o->patch = NULL;
    o->patches = o->patch_cap = o->drops = o->drop_cap = o->strays = 0;
    o->mapped = false;
}

static void drop_hold(run_t *r, obj_t *o)
{
    release(r, o->held_gaps);
    o->held_gaps = NULL;
    o->held_gap_count = 0;
    if (!o->hold) return;
    for (uint32_t c = 0; c < o->hold_chunks; ++c) release(r, o->hold[c]);
    release(r, o->hold);
    o->hold = NULL;
    o->hold_chunks = 0;
    o->held = false;
}

static bool grow(run_t *r, void **array, uint32_t *cap, uint32_t count, size_t item, uint32_t initial)
{
    if (count < *cap) return true;
    uint32_t next = *cap ? *cap * 2 : initial;
    void *grown = zalloc(r, (size_t)next * item);
    if (!grown) return false;
    if (*array) memcpy(grown, *array, (size_t)count * item);
    release(r, *array);
    *array = grown;
    *cap = next;
    return true;
}

static bool add_header(run_t *r, uint32_t pix, const uint8_t *page, uint8_t flags)
{
    if (!grow(r, (void **)&r->hdrs, &r->hdr_cap, r->hdr_count, sizeof(hdr_t), 128)) return false;
    hdr_t *h = &r->hdrs[r->hdr_count++];
    memset(h, 0, sizeof(*h));
    h->pix = (uint16_t)pix;
    h->obj = (uint16_t)(le16(page) & ~SR_IX);
    h->size = le32(page + offsetof(spiffs_page_object_ix_header, size));
    const uint8_t *name = page + offsetof(spiffs_page_object_ix_header, name);
    size_t length = 0;
    while (length < SPIFFS_OBJ_NAME_LEN && name[length]) ++length;
    h->named = length > 1 && length < SPIFFS_OBJ_NAME_LEN && name[0] == '/';
    for (size_t i = 0; h->named && i < length; ++i)
        if (name[i] < 0x21 || name[i] > 0x7e || name[i] == '"' || name[i] == '\\') h->named = false;
    if (h->named) memcpy(h->name, name, length);
    h->hidden = !(flags & SPIFFS_PH_FLAG_IXDELE);
    return true;
}

static bool scan(run_t *r)
{
    r->hdr_count = r->ixpage_count = 0;
    for (uint32_t b = 0; b < r->blocks; ++b) {
        if (!going(r)) return false;
        if (!hal_read(r, SPIFFS_BLOCK_TO_PADDR(r->fs, b), r->block, r->block_size))
            return fail(r, "survey_read", SPIFFS_ERR_INTERNAL);
        for (uint32_t e = 0; e < r->entries; ++e) {
            uint32_t pix = b * r->ppb + 1 + e;
            const uint8_t *page = r->block + (1 + e) * r->page_size;
            pg_t *pg = &r->pages[pix];
            pg->lu = le16(r->block + e * 2);
            pg->obj = le16(page);
            pg->span = le16(page + 2);
            pg->flags = page[4];
            pg->erased = 1;
            for (uint32_t i = 0; i < r->page_size; ++i) {
                if (page[i] != 0xFF) {
                    pg->erased = 0;
                    break;
                }
            }
            if (!(pg->obj & SR_IX) || pg->obj == SPIFFS_OBJ_ID_FREE || !index_valid(pg->flags)) continue;
            bool ok = pg->span == 0 ? add_header(r, pix, page, pg->flags)
                                    : grow(r, (void **)&r->ixpages, &r->ixpage_cap, r->ixpage_count,
                                           sizeof(uint16_t), 256);
            if (ok && pg->span) r->ixpages[r->ixpage_count++] = (uint16_t)pix;
            if (!ok) return fail(r, "survey_memory", SPIFFS_ERR_INTERNAL);
        }
    }
    return true;
}

/* ---- mapping -------------------------------------------------------- */
static bool perfect_data(const run_t *r, uint16_t obj, uint32_t span, uint16_t pix)
{
    if (!page_ok(r, pix)) return false;
    const pg_t *pg = &r->pages[pix];
    return data_valid(pg->flags) && pg->obj == obj && pg->span == span && pg->lu == obj;
}

static int32_t imperfect_code(const run_t *r, uint16_t obj, uint32_t span, uint16_t pix)
{
    if (pix == SR_NO_PAGE) return SPIFFS_ERR_INDEX_REF_FREE;
    if (pix >= r->total_pages) return SPIFFS_ERR_INDEX_REF_INVALID;
    if (pix % r->ppb == 0) return SPIFFS_ERR_INDEX_REF_LU;
    const pg_t *pg = &r->pages[pix];
    /* SPIFFS_VALIDATE_DATA's order, then the object id it does not check. */
    if (pg->flags & SPIFFS_PH_FLAG_USED) return SPIFFS_ERR_IS_FREE;
    if (!(pg->flags & SPIFFS_PH_FLAG_DELET)) return SPIFFS_ERR_DELETED;
    if (pg->flags & SPIFFS_PH_FLAG_FINAL) return SPIFFS_ERR_NOT_FINALIZED;
    if (!(pg->flags & SPIFFS_PH_FLAG_INDEX)) return SPIFFS_ERR_IS_INDEX;
    if (pg->span != span) return SPIFFS_ERR_DATA_SPAN_MISMATCH;
    if (pg->obj != obj) return SPIFFS_ERR_CHECK_OBJ_ID_MISM;
    return SR_ERR_LOOKUP_MISMATCH;
}

static uint32_t span_length(const obj_t *o, uint32_t span)
{
    uint32_t start = span * SRP_SPAN_BYTES;
    return o->size - start < SRP_SPAN_BYTES ? o->size - start : SRP_SPAN_BYTES;
}
static uint32_t first_span_of(const run_t *r, uint32_t ix_span)
{
    return ix_span == 0 ? 0 : r->hdr_ix + (ix_span - 1) * r->ix_len;
}
static uint32_t entries_of(const run_t *r, uint32_t ix_span) { return ix_span == 0 ? r->hdr_ix : r->ix_len; }
static uint32_t table_offset(uint32_t ix_span)
{
    return ix_span == 0 ? sizeof(spiffs_page_object_ix_header) : sizeof(spiffs_page_object_ix);
}

/* Two points per perfect reference below `spans`; a used entry past the end
 * costs one (an interrupted append left it, and the check would chase it). */
static bool score_index(run_t *r, uint16_t obj, uint32_t ix_span, uint16_t pix, uint32_t spans, int32_t *score)
{
    if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, pix), r->page, r->page_size)) return false;
    uint32_t first = first_span_of(r, ix_span), entries = entries_of(r, ix_span);
    const uint8_t *table = r->page + table_offset(ix_span);
    int32_t s = 0;
    for (uint32_t i = 0; i < entries; ++i) {
        uint16_t ref = le16(table + i * 2);
        if (first + i < spans) s += perfect_data(r, obj, first + i, ref) ? 2 : 0;
        else if (ref != SR_NO_PAGE) s -= 1;
    }
    *score = s;
    return true;
}

static bool temporary_name(const char *name)
{
    static const char *const suffixes[] = {".bak", ".tmp", ".stage", ".backup", ".commit", ".producer", ".spare",
                                           SRP_RESTORE_SUFFIX};
    size_t length = strlen(name);
    for (size_t i = 0; i < sizeof(suffixes) / sizeof(suffixes[0]); ++i) {
        size_t n = strlen(suffixes[i]);
        if (length > n && !strcmp(name + length - n, suffixes[i])) return true;
    }
    return false;
}

static bool push_drop(run_t *r, obj_t *o, uint16_t pix)
{
    for (uint32_t i = 0; i < o->drops; ++i)
        if (o->drop[i] == pix) return true;
    if (!grow(r, (void **)&o->drop, &o->drop_cap, o->drops, sizeof(uint16_t), 8)) return false;
    o->drop[o->drops++] = pix;
    return true;
}

/* Steps of the patch lookups and mask scans, for the harness's bound on the
 * engine's CPU work (a file with thousands of damaged spans once took
 * billions of linear-search steps). */
static uint32_t s_cpu_steps;
static void cpu_step(uint32_t n) { s_cpu_steps = UINT32_MAX - s_cpu_steps < n ? UINT32_MAX : s_cpu_steps + n; }

/* Patches stay sorted by span, one per span: every caller adds spans in
 * increasing order, and an out-of-order one is inserted in its place. */
static patch_t *push_patch(run_t *r, obj_t *o, uint32_t span)
{
    if (!grow(r, (void **)&o->patch, &o->patch_cap, o->patches, sizeof(patch_t), 4)) return NULL;
    uint32_t at = o->patches;
    while (at && o->patch[at - 1].span > span) --at;
    if (at < o->patches) memmove(&o->patch[at + 1], &o->patch[at], (o->patches - at) * sizeof(patch_t));
    o->patches++;
    patch_t *p = &o->patch[at];
    memset(p, 0, sizeof(*p));
    p->span = span;
    return p;
}

/* The first patch at or after `span` (o->patches when none). */
static uint32_t patch_at_or_after(const obj_t *o, uint32_t span)
{
    uint32_t low = 0, high = o->patches;
    while (low < high) {
        uint32_t mid = low + (high - low) / 2;
        cpu_step(1);
        if (o->patch[mid].span < span) low = mid + 1;
        else high = mid;
    }
    return low;
}

static patch_t *find_patch(obj_t *o, uint32_t span)
{
    uint32_t i = patch_at_or_after(o, span);
    return i < o->patches && o->patch[i].span == span ? &o->patch[i] : NULL;
}

static bool read_payload(run_t *r, uint16_t pix, uint8_t *out, uint32_t length)
{
    return hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, pix) + sizeof(spiffs_page_header), out, length);
}

/* Finalized data pages of this object by span, in page order. Built once per
 * object that has an imperfect span. */
static bool build_candidates(run_t *r, const obj_t *o)
{
    if (!r->cand_next) {
        r->cand_next = zalloc(r, r->total_pages * sizeof(uint16_t));
        if (!r->cand_next) return false;
    }
    release(r, r->cand_valid);
    r->cand_valid = zalloc(r, (o->spans ? o->spans : 1) * sizeof(uint16_t));
    if (!r->cand_valid) return false;
    memset(r->cand_valid, 0xFF, (o->spans ? o->spans : 1) * sizeof(uint16_t));
    for (uint32_t pix = r->total_pages; pix-- > 0;) {
        if (pix % r->ppb == 0) continue;
        const pg_t *pg = &r->pages[pix];
        if (pg->obj != o->obj || pg->span >= o->spans || !data_valid(pg->flags)) continue;
        r->cand_next[pix] = r->cand_valid[pg->span];
        r->cand_valid[pg->span] = (uint16_t)pix;
    }
    return true;
}

/* A finalized data page of this object and span, flagged deleted or not.
 * An unfinalized page is never a source: its write did not finish (a page
 * move or modify finalizes a data page once its bytes are on flash), and
 * when an index names one, the page the index named was erased and its
 * place reused. An append finalizes a new page before its bytes, but only
 * the spans below a committed size are read here, and the size is
 * committed after them (rebuild_salvage reads past it). */
static bool own_data_page(const pg_t *pg, uint16_t obj, uint32_t span)
{
    return pg->obj == obj && pg->span == span && (pg->flags & SPIFFS_PH_FLAG_INDEX) &&
        !(pg->flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL));
}

/* Merges one source's bytes into p->data byte by byte: a byte one source
 * holds programmed and another still erased (0xFF), or programmed with fewer
 * bits, is the more programmed one (a copy taken before later appends); two
 * values neither of which contains the other's bits disagree. */
static void merge_source(patch_t *p, const uint8_t *bytes, uint32_t length, uint8_t *conflict, bool first)
{
    for (uint32_t i = 0; i < length; ++i) {
        uint8_t a = p->data[i], b = bytes[i];
        if (first) p->data[i] = b;
        else if ((a & b) == a) continue;
        else if ((a & b) == b) p->data[i] = b;
        else set_bit(conflict, i);
    }
}

/* Bytes of an imperfect span, from the pages SPIFFS itself would trust as
 * that span: the page the index names, when its header is this object's
 * finalized data page of this span (flagged deleted or with a wrong lookup
 * entry: 13FL's head), and finalized copies whose lookup entry is this
 * object (an interrupted GC move or page move). A page flagged deleted that
 * the index does not name is never a source: SPIFFS reuses a removed file's
 * object id, so it may hold an earlier file's bytes. A byte the sources
 * disagree on, a byte no source holds, and a text byte that reads erased are
 * gaps. */
static bool resolve_span(run_t *r, obj_t *o, uint32_t span, uint16_t ref, int32_t code, patch_t *p)
{
    uint32_t length = span_length(o, span);
    uint8_t candidate[SRP_SPAN_BYTES], conflict[(SRP_SPAN_BYTES + 7) / 8];
    memset(conflict, 0, sizeof(conflict));
    p->span = span;
    p->length = (uint16_t)length;
    p->code = code;
    p->kind = PATCH_COPY;
    p->source = SOURCE_FILL;
    p->from = SR_NO_PAGE;
    memset(p->data, 0xFF, sizeof(p->data));
    bool last_of_append = span + 1 == o->spans; /* any file is appended in place */
    uint32_t sources = 0;
    uint16_t refer = SR_NO_PAGE;
    if (page_ok(r, ref) && own_data_page(&r->pages[ref], o->obj, span)) {
        if (!read_payload(r, ref, candidate, length)) return false;
        merge_source(p, candidate, length, conflict, true);
        sources++;
        p->from = ref;
        p->source = (r->pages[ref].flags & SPIFFS_PH_FLAG_DELET) ? SOURCE_RELOCATE : SOURCE_DELETED;
    }
    for (uint16_t pix = r->cand_valid[span]; pix != SR_NO_PAGE; pix = r->cand_next[pix]) {
        if (pix == ref || r->pages[pix].lu != o->obj) continue;
        if (!read_payload(r, pix, candidate, length)) return false;
        merge_source(p, candidate, length, conflict, sources == 0);
        if (!sources) {
            p->from = pix;
            p->source = SOURCE_ALTERNATE;
        }
        if (refer == SR_NO_PAGE && perfect_data(r, o->obj, span, pix)) refer = pix;
        sources++;
    }
    uint32_t unreadable = 0;
    bool disagree = false;
    for (uint32_t i = 0; i < length; ++i) {
        disagree |= bit(conflict, i);
        if (!sources || bit(conflict, i) || (text_fill(o) && p->data[i] == 0xFF)) {
            set_bit(p->mask, i);
            unreadable++;
        }
    }
    if (unreadable == length) p->source = SOURCE_FILL;
    else if (unreadable) p->source = SOURCE_PARTIAL;
    if (disagree) p->code = SR_ERR_SOURCES_DISAGREE;
    if (!unreadable && refer != SR_NO_PAGE && !last_of_append) {
        /* A perfect copy that holds exactly these bytes is referenced as it is. */
        if (!read_payload(r, refer, candidate, length)) return false;
        if (!memcmp(candidate, p->data, length)) {
            p->kind = PATCH_REFER;
            p->refer = refer;
        }
    }
    return true;
}

/* Maps one object as SPIFFS reads it and plans what makes it consistent. */
static bool map_object(run_t *r, obj_t *o)
{
    uint32_t size = o->size == SPIFFS_UNDEFINED_LEN ? 0 : o->size;
    o->size = size;
    o->spans = (size + SRP_SPAN_BYTES - 1) / SRP_SPAN_BYTES;
    o->ix_spans = o->spans ? SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, o->spans - 1) + 1 : 1;
    o->ix = zalloc(r, o->ix_spans * sizeof(uint16_t));
    o->ix_rewrite = zalloc(r, o->ix_spans);
    o->ref = zalloc(r, (o->spans ? o->spans : 1) * sizeof(uint16_t));
    o->perfect = zalloc(r, o->spans ? o->spans : 1);
    if (!o->ix || !o->ix_rewrite || !o->ref || !o->perfect) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
    o->ix[0] = o->header;
    if (r->pages[o->header].lu != (uint16_t)(o->obj | SR_IX) || o->size_recovered) o->ix_rewrite[0] = 1;
    /* One index page per later index span; duplicates and pages past the
     * end are dropped. */
    for (uint32_t j = 1; j < o->ix_spans; ++j) o->ix[j] = SR_NO_PAGE;
    for (uint32_t j = 1; j < o->ix_spans; ++j) {
        int32_t best_score = 0;
        for (uint32_t i = 0; i < r->ixpage_count; ++i) {
            uint16_t pix = r->ixpages[i];
            const pg_t *pg = &r->pages[pix];
            if ((uint16_t)(pg->obj & ~SR_IX) != o->obj || pg->span != j) continue;
            int32_t score;
            if (!score_index(r, o->obj, j, pix, o->spans, &score)) return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
            if (pg->lu == (uint16_t)(o->obj | SR_IX)) score += 1;
            if (o->ix[j] == SR_NO_PAGE || score > best_score) {
                if (o->ix[j] != SR_NO_PAGE && !push_drop(r, o, o->ix[j])) return false;
                o->ix[j] = pix;
                best_score = score;
            } else if (!push_drop(r, o, pix)) return false;
        }
        if (o->ix[j] == SR_NO_PAGE || r->pages[o->ix[j]].lu != (uint16_t)(o->obj | SR_IX)) o->ix_rewrite[j] = 1;
    }
    for (uint32_t i = 0; i < r->ixpage_count; ++i) {
        const pg_t *pg = &r->pages[r->ixpages[i]];
        if ((uint16_t)(pg->obj & ~SR_IX) == o->obj && pg->span >= o->ix_spans && !push_drop(r, o, r->ixpages[i]))
            return false;
    }
    /* References span by span; an entry past the end is cleared, since the
     * next append programs its page index over it in place. */
    for (uint32_t j = 0; j < o->ix_spans; ++j) {
        uint32_t first = first_span_of(r, j), entries = entries_of(r, j);
        if (o->ix[j] == SR_NO_PAGE) {
            for (uint32_t i = 0; i < entries && first + i < o->spans; ++i) o->ref[first + i] = SR_NO_PAGE;
            continue;
        }
        if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, o->ix[j]), r->page, r->page_size))
            return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
        const uint8_t *table = r->page + table_offset(j);
        for (uint32_t i = 0; i < entries; ++i) {
            uint16_t ref = le16(table + i * 2);
            if (first + i < o->spans) o->ref[first + i] = ref;
            else if (ref != SR_NO_PAGE) o->ix_rewrite[j] = 1;
        }
    }
    bool candidates = false;
    uint8_t data[SRP_SPAN_BYTES];
    for (uint32_t k = 0; k < o->spans; ++k) {
        if (k % 64 == 0 && !going(r)) return false;
        uint16_t ref = o->ref[k];
        uint32_t length = span_length(o, k);
        int32_t code = 0;
        if (o->ix[SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, k)] == SR_NO_PAGE) code = SPIFFS_ERR_NOT_FOUND;
        else if (!perfect_data(r, o->obj, k, ref)) code = imperfect_code(r, o->obj, k, ref);
        else if (text_fill(o)) {
            if (!read_payload(r, ref, data, length)) return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
            if (memchr(data, 0xFF, length)) code = SR_ERR_ERASED_CONTENT;
        }
        if (!code) {
            o->perfect[k] = 1;
            continue;
        }
        if (!candidates) {
            if (!build_candidates(r, o)) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
            candidates = true;
        }
        patch_t *p = push_patch(r, o, k);
        if (!p) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
        if (!resolve_span(r, o, k, ref, code, p)) return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
        o->ix_rewrite[SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, k)] = 1;
#ifdef SR_TRACE
        {
            char line[160];
            snprintf(line, sizeof(line), "%s span %lu ref %04x code %ld source %u from %04x kind %u", o->name,
                     (unsigned long)k, ref, (long)code, p->source, p->from, p->kind);
            logf_line(r, "DEBUG", "STORAGE_REPAIR_SPAN", line);
        }
#endif
    }
    /* The last page of a file must be erased past its end, or the next
     * append is ANDed into whatever an interrupted one left there: queue
     * files, and queue segments (dq_append writes at the end with "r+b"),
     * the command inbox and every other file SPIFFS appends to in place. */
    if (o->spans && o->size % SRP_SPAN_BYTES && o->perfect[o->spans - 1]) {
        uint32_t k = o->spans - 1, used = o->size % SRP_SPAN_BYTES;
        uint8_t erased[SRP_SPAN_BYTES];
        if (!read_payload(r, o->ref[k], data, SRP_SPAN_BYTES)) return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
        memset(erased, 0xFF, sizeof(erased));
        if (memcmp(data + used, erased, SRP_SPAN_BYTES - used)) {
            patch_t *p = push_patch(r, o, k);
            if (!p) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
            p->length = (uint16_t)used;
            p->kind = PATCH_COPY;
            p->source = SOURCE_TAIL;
            p->from = o->ref[k];
            memcpy(p->data, data, used);
            o->perfect[k] = 0;
            o->ix_rewrite[o->ix_spans - 1] = 1;
        }
    }
    o->needs_work = o->drops != 0;
    for (uint32_t j = 0; j < o->ix_spans; ++j) o->needs_work |= o->ix_rewrite[j] != 0;
    o->consistent = !o->needs_work;
    o->mapped = true;
    return true;
}

/* ---- torn header sizes ---------------------------------------------- */
/* 1 + the highest span any index entry of this object names: the chosen
 * header's table and every valid index page of the object (*own: the
 * header's table alone). */
static bool entry_trace(run_t *r, uint16_t obj, uint16_t header, uint32_t *top, uint32_t *own)
{
    *top = *own = 0;
    for (uint32_t i = 0; i <= r->ixpage_count; ++i) {
        uint16_t pix = i == r->ixpage_count ? header : r->ixpages[i];
        if (i < r->ixpage_count && (uint16_t)(r->pages[pix].obj & ~SR_IX) != obj) continue;
        uint32_t j = i == r->ixpage_count ? 0 : r->pages[pix].span;
        if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, pix), r->page, r->page_size)) return false;
        const uint8_t *table = r->page + table_offset(j);
        uint32_t first = first_span_of(r, j), entries = entries_of(r, j);
        for (uint32_t e = entries; e-- > 0;) {
            if (le16(table + e * 2) == SR_NO_PAGE) continue;
            if (first + e + 1 > *top) *top = first + e + 1;
            if (i == r->ixpage_count) *own = first + e + 1;
            break;
        }
    }
    return true;
}

/* Where the bytes of a data span's page end: the length before its trailing
 * erased (0xFF) bytes, over the finalized, undeleted pages of the object and
 * span whose lookup entry agrees (the most programmed one); 0 when there is
 * none. A page whose lookup entry was deleted may be an earlier file's that
 * reused the object id. */
static bool span_end(run_t *r, uint16_t obj, uint32_t span, uint32_t *used)
{
    uint8_t data[SRP_SPAN_BYTES];
    *used = 0;
    for (uint32_t pix = 0; pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        if (pix % r->ppb == 0 || pg->obj != obj || pg->span != span || pg->lu != obj || !data_valid(pg->flags))
            continue;
        if (!read_payload(r, (uint16_t)pix, data, SRP_SPAN_BYTES)) return false;
        uint32_t n = SRP_SPAN_BYTES;
        while (n && data[n - 1] == 0xFF) --n;
        if (n > *used) *used = n;
    }
    return true;
}

/* SPIFFS programs a new or truncated file's header (size 0xFFFFFFFF) in
 * place on its first append, after the data pages. A reset there tears the
 * size: every bit the real size clears may still be set, so the header can
 * claim megabytes no page holds, and SPIFFS can then neither read, append
 * to, truncate nor remove the file. A size that claims spans nothing on flash
 * traces (no finalized data page, no index entry) and contains the bits of a
 * size inside the last traced span is such a tear; the real size is where the
 * last traced page's bytes end (a text file never holds 0xFF), or for binary
 * files the smallest such size at or after the last programmed byte. A text
 * file's torn size can also stay inside its last span (only low bits left
 * set): its last page then ends in erased bytes the size claims, which no
 * committed append leaves (SPIFFS commits a size only after the bytes), so
 * the real size is again where the bytes end when the torn size contains it
 * bitwise. A binary file's 0xFF bytes are data: inside its last span it is
 * left as it is. */
static bool recover_size(run_t *r, const hdr_t *h, srp_kind_t kind, uint32_t *out)
{
    bool undefined = h->size == SPIFFS_UNDEFINED_LEN;
    *out = undefined ? 0 : h->size;
    uint64_t spans = undefined ? 0 : ((uint64_t)h->size + SRP_SPAN_BYTES - 1) / SRP_SPAN_BYTES;
    bool text = kind.fill == SRP_FILL_JSON_ROWS || kind.fill == SRP_FILL_ID_LINES;
    /* An empty file's header holds no entries: entries with the size still
     * 0xFFFFFFFF are a first append whose header program tore before the
     * size took a bit. */
    bool impossible = undefined || h->size > r->fs->cfg.phys_size;
    uint32_t traced = r->span_top[h->obj & (SR_IDS - 1)];
    uint32_t entries = 0, own = 0, used = 0;
    if (!impossible && spans < traced) return false;
    if (!impossible && spans == traced) {
        /* Inside the last span: only a text file's trailing erased bytes,
         * and only when no index entry names a span past it. */
        if (!text || !span_end(r, h->obj, traced - 1, &used)) return false;
        uint32_t real = (traced - 1) * SRP_SPAN_BYTES + used;
        if (!used || real >= h->size || (h->size & real) != real) return false;
        if (!entry_trace(r, h->obj, h->pix, &entries, &own) || entries > traced) return false;
        *out = real;
        return true;
    }
    if (!entry_trace(r, h->obj, h->pix, &entries, &own)) return false;
    if (undefined && !own) return false;
    if (entries > traced) traced = entries;
    if (!impossible && spans <= traced) return false;
    if (!traced) {
        if (!impossible) return false;
        *out = 0;
        return true;
    }
    uint32_t last = traced - 1;
    if (!span_end(r, h->obj, last, &used)) return false;
    if (!used) used = 1;
    uint32_t low = last * SRP_SPAN_BYTES + used, high = last * SRP_SPAN_BYTES + SRP_SPAN_BYTES, chosen = 0;
    for (uint32_t s = low; s <= high && !chosen; ++s) {
        if ((h->size & s) == s) chosen = s;
        if (text) break;
    }
    if (!chosen && !impossible) return false;
    *out = chosen ? chosen : low;
    return true;
}

static bool header_better(uint32_t size_a, const hdr_t *a, int32_t score_a, uint32_t size_b, const hdr_t *b,
                          int32_t score_b)
{
    if (score_a != score_b) return score_a > score_b;
    if (size_a != size_b) return size_a > size_b;
    bool temp_a = temporary_name(a->name), temp_b = temporary_name(b->name);
    if (temp_a != temp_b) return !temp_a;
    return a->pix < b->pix;
}

static void move_hold(obj_t *to, obj_t *from)
{
    to->hold = from->hold;
    to->hold_chunks = from->hold_chunks;
    to->hold_size = from->hold_size;
    to->held_gaps = from->held_gaps;
    to->held_gap_count = from->held_gap_count;
    to->held = from->held;
    to->crc = from->crc;
    to->actions |= from->actions;
    to->gaps = from->gaps;
    to->gap_bytes = from->gap_bytes;
    to->rewritten = from->rewritten;
    to->unrecordable = from->unrecordable;
    to->retired = from->retired;
    from->hold = NULL;
    from->hold_chunks = 0;
    from->held_gaps = NULL;
    from->held_gap_count = 0;
    from->held = false;
}

/* The size an object is mapped with: its header's, or the recovered one. */
static void apply_size(run_t *r, obj_t *o, const hdr_t *h)
{
    uint32_t size = 0;
    o->size_recovered = recover_size(r, h, o->kind, &size);
    o->size = size;
    o->header_size = h->size;
    /* An expendable with a torn size goes by object id instead (the probe
     * recreates its own file). */
    o->plausible = h->named && (h->size == SPIFFS_UNDEFINED_LEN || h->size <= r->fs->cfg.phys_size || o->size_recovered);
    if (o->size_recovered && o->kind.cls == SRP_CLASS_EXPENDABLE) {
        o->plausible = false;
        o->size_recovered = false;
        o->size = h->size;
    }
}

/* The repair's rebuild writes whole data pages (SR_COPY_BYTES is 16 of them)
 * and SPIFFS commits the size after each write: a reset inside one leaves
 * pages of the next spans past the committed size. SPIFFS's append writes a
 * new data page's header finalized before its bytes (spiffs_object_append,
 * "finalize immediately"), so the page being written when the reset came
 * may hold a torn prefix; every earlier one was complete before the next
 * page's lookup entry was written. Spans past the committed size count as
 * the rebuild's while the next span has a finalized page too, up to the
 * original's size: the last page written is left (its bytes become part of
 * the recorded tail). */
static uint32_t rebuild_salvage(run_t *r, const obj_t *o, const srp_manifest_file_t *f)
{
    uint32_t size = o->size, top = r->span_top[o->obj & (SR_IDS - 1)];
    if ((f->flags & SRP_MANIFEST_ADOPTED) || size % SRP_SPAN_BYTES) return size;
    for (uint32_t span = size / SRP_SPAN_BYTES; size < f->size && span + 1 < top; ++span) {
        uint32_t want = f->size - size < SRP_SPAN_BYTES ? f->size - size : SRP_SPAN_BYTES, used = 0;
        if (!span_end(r, o->obj, span, &used) || used < want) break;
        size += want;
    }
    return size;
}

/* Builds the object list: each visible object with one chosen header (its
 * other visible headers are dropped: SPIFFS's page move or rename stopped
 * before deleting the old one), and hidden ones (a removal in progress) that
 * only a manifest can claim. Holds and per-file results carry over by name. */
static bool build_objects(run_t *r)
{
    obj_t *old = r->objs;
    uint32_t old_count = r->obj_count;
    r->objs = zalloc(r, SR_MAX_OBJECTS * sizeof(obj_t));
    r->obj_count = 0;
    if (!r->objs) {
        r->objs = old;
        r->obj_count = old_count;
        return fail(r, "objects_memory", SPIFFS_ERR_INTERNAL);
    }
    memset(r->span_top, 0, SR_IDS * sizeof(uint16_t));
    for (uint32_t pix = 0; pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        /* A span no page index of the partition can reach is a header a
         * reset tore (its span bytes left 0xFF): it traces nothing, and
         * 0xFFFF + 1 would wrap the 16-bit top to 0. */
        if (pix % r->ppb == 0 || (pg->obj & SR_IX) || pg->obj == SPIFFS_OBJ_ID_FREE || pg->lu != pg->obj ||
            !data_valid(pg->flags) || pg->span >= r->total_pages)
            continue;
        uint16_t *top = &r->span_top[pg->obj & (SR_IDS - 1)];
        if (pg->span + 1U > *top) *top = (uint16_t)(pg->span + 1U);
    }
    bool ok = true;
    for (uint32_t i = 0; ok && i < r->hdr_count; ++i) {
        const hdr_t *h = &r->hdrs[i];
        bool seen = false;
        for (uint32_t k = 0; k < r->obj_count && !seen; ++k) seen = r->objs[k].obj == h->obj && !r->objs[k].hidden;
        if (seen) continue;
        bool any_visible = false;
        for (uint32_t k = i; k < r->hdr_count; ++k)
            if (r->hdrs[k].obj == h->obj && !r->hdrs[k].hidden) any_visible = true;
        if (!any_visible) {
            /* Each hidden header is its own candidate for a manifest. */
            if (r->obj_count == SR_MAX_OBJECTS) break;
            obj_t *o = &r->objs[r->obj_count++];
            memset(o, 0, sizeof(*o));
            o->obj = h->obj;
            o->header = h->pix;
            o->hidden = true;
            memcpy(o->name, h->name, sizeof(o->name));
            o->kind = srp_classify(o->name);
            apply_size(r, o, h);
            continue;
        }
        const hdr_t *best = NULL;
        int32_t best_score = 0;
        uint32_t best_size = 0;
        srp_kind_t kind = srp_classify(h->name);
        for (uint32_t k = i; k < r->hdr_count; ++k) {
            const hdr_t *c = &r->hdrs[k];
            if (c->obj != h->obj || c->hidden) continue;
            uint32_t size = 0;
            (void)recover_size(r, c, kind, &size);
            int32_t score = 0;
            if (!score_index(r, h->obj, 0, c->pix, (size + SRP_SPAN_BYTES - 1) / SRP_SPAN_BYTES, &score)) {
                ok = fail(r, "objects_read", SPIFFS_ERR_INTERNAL);
                break;
            }
            if (!best || header_better(size, c, score, best_size, best, best_score)) {
                best = c;
                best_score = score;
                best_size = size;
            }
        }
        if (!ok || !best) break;
        if (r->obj_count == SR_MAX_OBJECTS) {
            ok = fail(r, "objects_limit", SPIFFS_ERR_INTERNAL);
            break;
        }
        obj_t *o = &r->objs[r->obj_count++];
        memset(o, 0, sizeof(*o));
        o->obj = h->obj;
        o->header = best->pix;
        memcpy(o->name, best->name, sizeof(o->name));
        o->kind = srp_classify(o->name);
        srp_manifest_file_t *own = rebuild_entry(r, o->name, o->obj);
        if (own) o->kind = srp_classify(own->name);
        apply_size(r, o, best);
        if (own && o->plausible) {
            /* Its pages past the committed size are its bytes too: the
             * header takes the size they end at. */
            uint32_t salvaged = rebuild_salvage(r, o, own);
            if (salvaged > o->size) {
                o->size = salvaged;
                o->size_recovered = true;
            }
        }
        for (uint32_t k = i; ok && k < r->hdr_count; ++k) {
            const hdr_t *c = &r->hdrs[k];
            if (c->obj == h->obj && !c->hidden && c->pix != best->pix) ok = push_drop(r, o, c->pix);
        }
    }
    /* Carry holds over; a held file the store no longer shows keeps its hold
     * under a placeholder until it is restored. */
    for (uint32_t i = 0; old && i < old_count; ++i) {
        obj_t *prev = &old[i];
        release_maps(r, prev);
        if (!prev->hold) continue;
        obj_t *match = NULL;
        for (uint32_t k = 0; k < r->obj_count && !match; ++k)
            if (live(&r->objs[k]) && !strcmp(r->objs[k].name, prev->name)) match = &r->objs[k];
        if (!match && r->obj_count < SR_MAX_OBJECTS) {
            match = &r->objs[r->obj_count++];
            memset(match, 0, sizeof(*match));
            memcpy(match->name, prev->name, sizeof(match->name));
            match->kind = prev->kind;
            match->size = prev->hold_size;
            match->header = SR_NO_PAGE;
            match->obj = SR_NO_PAGE;
            match->hidden = true;
        }
        if (match && !match->hold) move_hold(match, prev);
        else drop_hold(r, prev);
    }
    release(r, old);
    for (uint32_t i = 0; ok && i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        /* Hidden objects are mapped only when a manifest claims them. */
        if (live(o)) ok = map_object(r, o);
    }
    /* Stray pages of a live file: any page whose lookup entry and header name
     * it and that is none of its chosen header, index and data pages,
     * whatever its flag bits. A data page past the end is an append that
     * never committed: the check would link it into the index (FIX_INDEX)
     * and the next append programs its own page index over that entry in
     * place. A page whose header write tore (flags 0xFD: finalized but still
     * marked unused) is ignored by the check, but SPIFFS's GC moves every
     * page whose lookup entry names the file and points that span's index
     * entry at the copy. A copy of a span whose referenced page is perfect is
     * stale. Pages of an imperfect span stay until it is rewritten: they may
     * be its bytes. */
    uint16_t *owner = ok ? zalloc(r, SR_IDS * sizeof(uint16_t)) : NULL;
    if (ok && !owner) ok = fail(r, "objects_memory", SPIFFS_ERR_INTERNAL);
    for (uint32_t k = 0; ok && k < r->obj_count; ++k)
        if (live(&r->objs[k]) && r->objs[k].mapped) owner[r->objs[k].obj & (SR_IDS - 1)] = (uint16_t)(k + 1);
    for (uint32_t pix = 0; ok && pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        if (pix % r->ppb == 0 || pg->lu == SPIFFS_OBJ_ID_FREE || pg->lu == SPIFFS_OBJ_ID_DELETED || pg->lu != pg->obj)
            continue;
        uint16_t index = owner[pg->lu & (SR_IDS - 1)];
        if (!index) continue;
        obj_t *o = &r->objs[index - 1];
        if (pg->lu & SR_IX) {
            if (pix == o->header) continue;
            if (pg->span && pg->span < o->ix_spans && o->ix[pg->span] == pix) continue;
        } else if (pg->span < o->spans && (!o->perfect[pg->span] || o->ref[pg->span] == pix)) {
            continue;
        }
        uint32_t before = o->drops;
        ok = push_drop(r, o, (uint16_t)pix);
        if (o->drops > before) o->strays++;
        o->needs_work = true;
        o->consistent = false;
    }
    release(r, owner);
    /* Index pages past span 0 whose file is gone (a removal stopped part way:
     * SPIFFS flags the header first and frees from the end). The check never
     * frees them: its index pass skips valid index pages and its page pass
     * cannot find the hidden header to delete, so their data pages stay
     * referenced and leak for good. */
    r->orphan_count = 0;
    for (uint32_t i = 0; ok && i < r->ixpage_count; ++i) {
        uint16_t obj = (uint16_t)(r->pages[r->ixpages[i]].obj & ~SR_IX);
        bool owned = false;
        for (uint32_t k = 0; k < r->obj_count && !owned; ++k) owned = live(&r->objs[k]) && r->objs[k].obj == obj;
        if (owned) continue;
        ok = grow(r, (void **)&r->orphans, &r->orphan_cap, r->orphan_count, sizeof(uint16_t), 64);
        if (ok) r->orphans[r->orphan_count++] = r->ixpages[i];
        else fail(r, "objects_memory", SPIFFS_ERR_INTERNAL);
    }
    return ok;
}

static bool survey(run_t *r)
{
    return !r->failed && scan(r) && build_objects(r);
}

static obj_t *find_live(run_t *r, const char *name)
{
    for (uint32_t i = 0; i < r->obj_count; ++i)
        if (live(&r->objs[i]) && !strcmp(r->objs[i].name, name)) return &r->objs[i];
    return NULL;
}

/* ---- content ------------------------------------------------------- */
/* Bytes of a mapped object as they read after normalization; false when an
 * unfilled gap intersects. */
static bool object_read(run_t *r, obj_t *o, uint32_t offset, uint8_t *out, uint32_t length)
{
    if (!o->mapped || (uint64_t)offset + length > o->size) return false;
    while (length) {
        uint32_t span = offset / SRP_SPAN_BYTES, within = offset % SRP_SPAN_BYTES;
        uint32_t n = SRP_SPAN_BYTES - within;
        if (n > length) n = length;
        if (o->perfect[span]) {
            if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, o->ref[span]) + sizeof(spiffs_page_header) + within, out, n))
                return false;
        } else {
            patch_t *p = find_patch(o, span);
            if (!p) return false;
            for (uint32_t i = 0; !p->filled && i < n; ++i)
                if (bit(p->mask, within + i)) return false;
            memcpy(out, p->data + within, n);
        }
        out += n;
        offset += n;
        length -= n;
    }
    return true;
}

static bool held_read(const obj_t *o, uint32_t offset, uint8_t *out, uint32_t length)
{
    if (!o->hold || (uint64_t)offset + length > o->hold_size) return false;
    while (length) {
        uint32_t chunk = offset / SR_HOLD_CHUNK, within = offset % SR_HOLD_CHUNK;
        uint32_t n = SR_HOLD_CHUNK - within;
        if (n > length) n = length;
        memcpy(out, o->hold[chunk] + within, n);
        out += n;
        offset += n;
        length -= n;
    }
    return true;
}

static void held_write(obj_t *o, uint32_t offset, const uint8_t *in, uint32_t length)
{
    while (length) {
        uint32_t chunk = offset / SR_HOLD_CHUNK, within = offset % SR_HOLD_CHUNK;
        uint32_t n = SR_HOLD_CHUNK - within;
        if (n > length) n = length;
        memcpy(o->hold[chunk] + within, in, n);
        in += n;
        offset += n;
        length -= n;
    }
}

/* The next unreadable byte at or after offset, or o->size: patch by patch
 * (only an imperfect span has one), each one's mask scanned once. */
static uint32_t next_masked(obj_t *o, uint32_t offset)
{
    for (uint32_t i = patch_at_or_after(o, offset / SRP_SPAN_BYTES); i < o->patches && offset < o->size; ++i) {
        const patch_t *p = &o->patch[i];
        if (p->span >= o->spans) break;
        uint32_t start = p->span * SRP_SPAN_BYTES, end = start + span_length(o, p->span);
        if (o->perfect[p->span]) continue;
        for (uint32_t at = offset > start ? offset : start; at < end; ++at) {
            cpu_step(1);
            if (bit(p->mask, at - start)) return at;
        }
    }
    return o->size;
}

/* The end of the unreadable run starting at start. */
static uint32_t masked_end(obj_t *o, uint32_t start)
{
    uint32_t end = start;
    for (uint32_t i = patch_at_or_after(o, start / SRP_SPAN_BYTES); i < o->patches && end < o->size; ++i) {
        const patch_t *p = &o->patch[i];
        uint32_t first = p->span * SRP_SPAN_BYTES, last = first + span_length(o, p->span);
        if (p->span >= o->spans || first > end || o->perfect[p->span]) break;
        while (end < last && bit(p->mask, end - first)) {
            cpu_step(1);
            ++end;
        }
        if (end < last) break;
    }
    return end;
}

/* Fills every unreadable run from the readable bytes around it (srp_fill):
 * back to the previous newline and on to the next, within a row's length. */
static bool fill_gaps(run_t *r, obj_t *o)
{
    uint8_t *window = NULL;
    for (uint32_t start = next_masked(o, 0); start < o->size; start = next_masked(o, start)) {
        uint32_t end = masked_end(o, start);
        uint32_t length = end - start;
        size_t prefix = 0, suffix = 0;
        bool suffix_complete = false;
        if (!window && !(window = zalloc(r, SR_ROW_WINDOW))) return false;
        uint8_t *prefix_bytes = window, *suffix_bytes = window + SR_ROW_WINDOW / 2;
        if (o->kind.fill == SRP_FILL_JSON_ROWS) {
            uint32_t from = start;
            uint8_t byte = 0;
            bool boundary = from == 0;
            while (from > 0 && start - from < SR_ROW_WINDOW / 2) {
                if (!object_read(r, o, from - 1, &byte, 1)) break;
                if (byte == '\n') {
                    boundary = true;
                    break;
                }
                --from;
            }
            if (from == 0) boundary = true;
            if (boundary && start > from && object_read(r, o, from, window, start - from)) prefix = start - from;
            while (suffix < SR_ROW_WINDOW / 2 && end + suffix < o->size &&
                   object_read(r, o, end + (uint32_t)suffix, suffix_bytes + suffix, 1)) {
                if (suffix_bytes[suffix++] == '\n') {
                    suffix_complete = true;
                    break;
                }
            }
        }
        uint8_t *fill = zalloc(r, length);
        if (!fill) {
            release(r, window);
            return false;
        }
        srp_fill(o->kind.fill, prefix_bytes, prefix, suffix_bytes, suffix, suffix_complete, fill, length);
        patch_t *q = NULL;
        for (uint32_t at = start; at < end; ++at) {
            if (!q || q->span != at / SRP_SPAN_BYTES) q = find_patch(o, at / SRP_SPAN_BYTES);
            q->data[at % SRP_SPAN_BYTES] = fill[at - start];
        }
        release(r, fill);
        start = end;
        pace(r);
    }
    release(r, window);
    for (uint32_t i = 0; i < o->patches; ++i) o->patch[i].filled = 1;
    return true;
}

/* ---- gap records ---------------------------------------------------- */
static void note_file(run_t *r, const obj_t *o)
{
    srr_report_t *rep = r->report;
    srr_report_file_t *f = NULL;
    for (uint32_t i = 0; i < rep->file_count && !f; ++i)
        if (!strcmp(rep->file[i].name, o->name)) f = &rep->file[i];
    if (!f) {
        if (rep->file_count == SRR_REPORT_FILES) return;
        f = &rep->file[rep->file_count++];
        memcpy(f->name, o->name, sizeof(f->name));
        f->cls = (uint8_t)o->kind.cls;
    }
    f->actions |= o->actions;
    f->size = o->size;
    f->gaps = o->gaps;
    f->unreadable_bytes = o->gap_bytes;
    f->spans_rewritten = o->rewritten;
    f->blocked_spans = o->blocked_spans;
    f->blocked_offset = o->blocked_offset;
}

/* A range of this file this run already recorded as unreadable. */
static bool recorded_this_run(const run_t *r, const obj_t *o, uint32_t start, uint32_t end)
{
    for (uint32_t i = 0; i < r->gaps->count && i < SRP_MAX_GAPS; ++i) {
        const srp_gap_t *g = &r->gaps->gap[i];
        if (g->generation == r->state.generation && !strcmp(g->name, o->name) && g->offset <= start &&
            (uint64_t)g->offset + g->length >= end)
            return true;
    }
    return false;
}

static bool record_gap_flags(run_t *r, obj_t *o, uint32_t offset, uint32_t length, int32_t code,
                             srp_gap_cause_t cause, uint8_t flags)
{
    /* A range this run already recorded (a survey repeated after making
     * room) is not recorded twice. */
    for (uint32_t i = 0; i < r->gaps->count && i < SRP_MAX_GAPS; ++i) {
        const srp_gap_t *old = &r->gaps->gap[i];
        if (old->generation == r->state.generation && old->offset == offset && old->length == length &&
            old->cause == (uint8_t)cause && !strcmp(old->name, o->name))
            return true;
    }
    srp_gap_t gap;
    memset(&gap, 0, sizeof(gap));
    memcpy(gap.name, o->name, sizeof(gap.name));
    gap.cause = (uint8_t)cause;
    gap.cls = (uint8_t)o->kind.cls;
    gap.spiffs_error = code;
    gap.offset = offset;
    gap.length = length;
    gap.file_size = o->size;
    gap.generation = r->state.generation;
    gap.flags = flags;
    if (!srp_gaps_add(r->gaps, &gap)) {
        r->report->records_dropped++;
        return false;
    }
    r->report->gaps++;
    r->report->unreadable_bytes += length;
    o->gaps++;
    o->gap_bytes += length;
    o->actions |= SRR_FILE_GAPS;
    char message[160];
    snprintf(message, sizeof(message), "%s: %lu unreadable bytes at offset %lu (SPIFFS %ld, %s)", o->name,
             (unsigned long)length, (unsigned long)offset, (long)code, srp_gap_cause_name(cause));
    logf_line(r, "WARN", "STORAGE_REPAIR_GAP", message);
    return true;
}

static bool record_gap(run_t *r, obj_t *o, uint32_t offset, uint32_t length, int32_t code, srp_gap_cause_t cause)
{
    return record_gap_flags(r, o, offset, length, code, cause, 0);
}

/* Every unreadable run of a file, recorded before anything is written.
 * Runs less than a page apart (bytes a torn program left scattered) are one
 * record, flagged merged: its bounds cover every unreadable byte, and a few
 * readable ones, rather than a list of one-byte records that would fill the
 * journal. */
static bool record_salvage_gaps(run_t *r, obj_t *o)
{
    for (uint32_t start = next_masked(o, 0); start < o->size;) {
        uint32_t end = masked_end(o, start);
        bool merged = false;
        for (uint32_t next = next_masked(o, end); next < o->size && next - end < SRP_SPAN_BYTES;
             next = next_masked(o, end)) {
            end = masked_end(o, next);
            merged = true;
        }
        patch_t *p = find_patch(o, start / SRP_SPAN_BYTES);
        if (!recorded_this_run(r, o, start, end) &&
            !record_gap_flags(r, o, start, end - start, p ? p->code : 0, SRP_GAP_SALVAGE,
                              merged ? SRP_GAP_MERGED : 0)) {
            o->unrecordable = true;
            return false;
        }
        start = next_masked(o, end);
        pace(r);
    }
    return true;
}

/* Unreadable when the file was held (held_gaps is sorted by span). */
static bool held_gap(const obj_t *o, uint32_t offset)
{
    uint32_t span = offset / SRP_SPAN_BYTES, low = 0, high = o->held_gap_count;
    while (low < high) {
        uint32_t mid = low + (high - low) / 2;
        if (o->held_gaps[mid].span < span) low = mid + 1;
        else high = mid;
    }
    return low < o->held_gap_count && o->held_gaps[low].span == span &&
        bit(o->held_gaps[low].mask, offset % SRP_SPAN_BYTES);
}

/* A file held earlier in this run whose bytes a later survey cannot read
 * (a page that was a source got erased, or moved by SPIFFS's GC): every byte
 * that was readable when it was held comes from the hold, the readable bytes
 * a merged record's bounds cover included. The bytes nobody could read then
 * stay gaps. */
static void unmask_from_hold(obj_t *o)
{
    if (!o->hold || o->hold_size != o->size) return;
    for (uint32_t start = next_masked(o, 0); start < o->size;) {
        uint32_t end = masked_end(o, start);
        patch_t *p = NULL;
        for (uint32_t at = start; at < end; ++at) {
            cpu_step(1);
            if (held_gap(o, at)) continue;
            if (!p || p->span != at / SRP_SPAN_BYTES) p = find_patch(o, at / SRP_SPAN_BYTES);
            uint8_t byte;
            if (!p || !held_read(o, at, &byte, 1)) continue;
            p->data[at % SRP_SPAN_BYTES] = byte;
            clear_bit(p->mask, at % SRP_SPAN_BYTES);
            if (p->source == SOURCE_FILL || p->source == SOURCE_PARTIAL) p->source = SOURCE_HELD;
        }
        start = next_masked(o, end);
    }
}

/* What a hold cannot vouch for: the spans with unreadable bytes now, sorted
 * by span. */
static bool note_held_gaps(run_t *r, obj_t *o)
{
    uint32_t count = 0;
    for (uint32_t i = 0; i < o->patches; ++i)
        for (uint32_t b = 0; b < sizeof(o->patch[i].mask); ++b)
            if (o->patch[i].mask[b]) {
                ++count;
                break;
            }
    if (!count) return true;
    o->held_gaps = zalloc(r, count * sizeof(held_gap_t));
    if (!o->held_gaps) return false;
    for (uint32_t i = 0; i < o->patches; ++i) {
        const patch_t *p = &o->patch[i];
        bool any = false;
        for (uint32_t b = 0; b < sizeof(p->mask) && !any; ++b) any = p->mask[b] != 0;
        if (!any) continue;
        uint32_t at = o->held_gap_count++;
        while (at && o->held_gaps[at - 1].span > p->span) {
            o->held_gaps[at] = o->held_gaps[at - 1];
            --at;
        }
        o->held_gaps[at].span = p->span;
        memcpy(o->held_gaps[at].mask, p->mask, sizeof(p->mask));
    }
    return true;
}

/* ---- holds ---------------------------------------------------------- */
static bool take_hold(run_t *r, obj_t *o)
{
    uint32_t chunks = (o->size + SR_HOLD_CHUNK - 1) / SR_HOLD_CHUNK;
    drop_hold(r, o);
    o->hold = zalloc(r, (chunks ? chunks : 1) * sizeof(uint8_t *));
    if (!o->hold) return false;
    for (uint32_t c = 0; c < chunks; ++c) {
        uint32_t n = o->size - c * SR_HOLD_CHUNK < SR_HOLD_CHUNK ? o->size - c * SR_HOLD_CHUNK : SR_HOLD_CHUNK;
        o->hold[c] = r->ports->alloc(r->ports->context, n);
        o->hold_chunks = c + 1;
        if (!o->hold[c]) {
            o->hold_chunks = c;
            drop_hold(r, o);
            return false;
        }
    }
    o->hold_size = o->size;
    uint8_t span[SRP_SPAN_BYTES];
    uint32_t crc = 0;
    for (uint32_t k = 0; k < o->spans; ++k) {
        uint32_t length = span_length(o, k);
        if (!object_read(r, o, k * SRP_SPAN_BYTES, span, length)) {
            drop_hold(r, o);
            return false;
        }
        held_write(o, k * SRP_SPAN_BYTES, span, length);
        crc = srp_crc32(crc, span, length);
    }
    if (!note_held_gaps(r, o)) {
        drop_hold(r, o);
        return false;
    }
    o->crc = crc;
    o->held = true;
    return true;
}

/* Custody first: every unsettled-or-not byte of every custody file, in class
 * priority, as far as the budget holds whole files. */
static void hold_custody(run_t *r)
{
    srp_budget_file_t *files = zalloc(r, SR_MAX_OBJECTS * sizeof(srp_budget_file_t));
    uint32_t *map = zalloc(r, SR_MAX_OBJECTS * sizeof(uint32_t));
    uint32_t count = 0;
    for (uint32_t i = 0; files && map && i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || o->held || !custody(o) || !o->size) continue;
        files[count].size = o->size;
        files[count].cls = (uint8_t)o->kind.cls;
        map[count++] = i;
    }
    uint64_t budget = r->cfg->hold_budget > r->hold_used ? r->cfg->hold_budget - r->hold_used : 0;
    if (files) srp_budget(files, count, budget);
    for (uint32_t i = 0; i < count; ++i) {
        obj_t *o = &r->objs[map[i]];
        if (files[i].hold && take_hold(r, o)) {
            r->hold_used += o->size;
            r->report->files_held++;
            r->report->bytes_held += o->size;
        } else if (o->kind.cls != SRP_CLASS_DEDUPE && o->kind.cls != SRP_CLASS_UNKNOWN) {
            o->actions |= SRR_FILE_UNHELD;
            r->report->files_unheld_custody++;
        }
        pace(r);
    }
    release(r, files);
    release(r, map);
}

/* ---- writes --------------------------------------------------------- */
static bool write_lookup(run_t *r, uint32_t pix, uint16_t id)
{
    spiffs *fs = r->fs;
    u32_t addr = SPIFFS_BLOCK_TO_PADDR(fs, SPIFFS_BLOCK_FOR_PAGE(fs, pix)) +
        SPIFFS_OBJ_LOOKUP_ENTRY_FOR_PAGE(fs, pix) * sizeof(spiffs_obj_id);
    spiffs_obj_id value = id;
    r->wrote = true;
    return _spiffs_wr(fs, SPIFFS_OP_T_OBJ_LU | SPIFFS_OP_C_UPDT, 0, addr, sizeof(value), (u8_t *)&value) == SPIFFS_OK;
}

static bool delete_page(run_t *r, uint16_t pix)
{
    r->wrote = true;
    return spiffs_page_delete(r->fs, pix) == SPIFFS_OK;
}

/* A used page whose lookup entry still says free (the allocator can hand
 * it out and program over it), or a lookup entry naming another object than
 * its page does (a torn lookup write: NOR leaves some of the new bits
 * unprogrammed). SPIFFS_check handles the second badly: for a finalized data
 * page no index references it deletes the page after an index search that
 * overwrote the lookup page it is walking, and reads the rest of that block
 * through another block's lookup entries, deleting live pages. */
static bool mislabeled(const pg_t *pg)
{
    if (pg->lu == SPIFFS_OBJ_ID_FREE) return !pg->erased;
    return pg->lu != SPIFFS_OBJ_ID_DELETED && pg->lu != pg->obj;
}

static bool any_mislabeled(const run_t *r)
{
    for (uint32_t pix = 0; pix < r->total_pages; ++pix)
        if (pix % r->ppb && mislabeled(&r->pages[pix])) return true;
    return false;
}

/* A page the survey needs (a header, an index page, a reference or a byte
 * source) whose lookup entry says free gets its lookup entry; one whose
 * lookup entry names another object is left to normalization, which writes
 * its bytes to a new page. Every other mislabeled page is deleted. Runs
 * before the first page is allocated, and again after normalization. */
static bool lookup_hygiene(run_t *r)
{
    uint8_t *needed = zalloc(r, r->total_pages);
    uint8_t *hidden = zalloc(r, SR_IDS / 8U);
    if (!needed || !hidden) {
        release(r, needed);
        release(r, hidden);
        return fail(r, "hygiene_memory", SPIFFS_ERR_INTERNAL);
    }
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (o->header == SR_NO_PAGE || !o->plausible) continue;
        /* A hidden object (a removal in progress) matters only when an
         * unsettled manifest entry names it: then every page whose header
         * names it stays, for the resume to rebuild from. Any other is
         * garbage the check frees, and a mislabeled page of it would set
         * off the check's torn-lookup handling (1.2 item 7). */
        if (o->hidden) {
            srp_manifest_file_t *f = manifest_entry(r, o->name);
            if (!f || f->object_id != o->obj || (f->flags & SRP_MANIFEST_SETTLED)) continue;
            set_bit(hidden, o->obj & (SR_IDS - 1));
        }
        needed[o->header] = 1;
        for (uint32_t j = 0; o->mapped && j < o->ix_spans; ++j)
            if (o->ix[j] != SR_NO_PAGE) needed[o->ix[j]] = 1;
        for (uint32_t k = 0; o->mapped && k < o->spans; ++k)
            if (page_ok(r, o->ref[k])) needed[o->ref[k]] = 1;
        for (uint32_t k = 0; k < o->patches; ++k)
            if (page_ok(r, o->patch[k].from)) needed[o->patch[k].from] = 1;
    }
    for (uint32_t pix = 0; pix < r->total_pages; ++pix)
        if (pix % r->ppb && r->pages[pix].obj != SPIFFS_OBJ_ID_FREE && bit(hidden, r->pages[pix].obj & (SR_IDS - 1)))
            needed[pix] = 1;
    release(r, hidden);
    bool ok = true;
    for (uint32_t pix = 0; ok && pix < r->total_pages; ++pix) {
        if (pix % r->ppb == 0) continue;
        pg_t *pg = &r->pages[pix];
        if (!mislabeled(pg)) continue;
        if (pg->lu == SPIFFS_OBJ_ID_FREE) {
            bool used = !(pg->flags & SPIFFS_PH_FLAG_USED) && pg->obj != SPIFFS_OBJ_ID_FREE &&
                pg->obj != SPIFFS_OBJ_ID_DELETED;
            ok = needed[pix] && used ? write_lookup(r, pix, pg->obj) : delete_page(r, (uint16_t)pix);
        } else if (needed[pix]) {
            continue;
        } else {
            ok = delete_page(r, (uint16_t)pix);
        }
        if (ok) r->report->lookup_fixed++;
        pace(r);
    }
    release(r, needed);
    return ok || fail(r, "lookup_write", SPIFFS_ERR_INTERNAL);
}

static bool verify_page(run_t *r, uint16_t pix, const uint8_t *expect, uint32_t from, uint32_t length)
{
    uint8_t back[256];
    if (r->page_size > sizeof(back)) return false;
    return hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, pix), back, r->page_size) &&
        !memcmp(back + from, expect + from, length);
}

/* A new data page for a span: written unfinalized, finalized, read back. A
 * page that does not read back as written is deleted and another one used. */
static bool new_data_page(run_t *r, obj_t *o, uint32_t span, const uint8_t *data, uint32_t length, uint16_t *out)
{
    uint8_t expect[256];
    if (r->page_size > sizeof(expect)) return false;
    for (int attempt = 0; attempt < 3; ++attempt) {
        spiffs_page_header ph = {.obj_id = o->obj, .span_ix = (spiffs_span_ix)span, .flags = 0xFF};
        spiffs_page_ix pix;
        r->wrote = true;
        s32_t res = spiffs_page_allocate_data(r->fs, o->obj, &ph, (u8_t *)data, length, 0, 1, &pix);
        if (res != SPIFFS_OK) return fail(r, "allocate_data", res);
        memset(expect, 0xFF, sizeof(expect));
        memcpy(expect, &ph, sizeof(ph));
        memcpy(expect + sizeof(spiffs_page_header), data, length);
        if (verify_page(r, pix, expect, 0, r->page_size)) {
            *out = pix;
            return true;
        }
        if (spiffs_page_delete(r->fs, pix) != SPIFFS_OK) break;
    }
    return fail(r, "allocate_verify", SPIFFS_ERR_INTERNAL);
}

static bool rewrite_index(run_t *r, obj_t *o, uint32_t j, const uint16_t *target)
{
    uint8_t buffer[256];
    if (r->page_size > sizeof(buffer)) return false;
    uint32_t first = first_span_of(r, j), entries = entries_of(r, j);
    uint16_t old = o->ix[j];
    if (old != SR_NO_PAGE) {
        if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, old), buffer, r->page_size))
            return fail(r, "index_read", SPIFFS_ERR_INTERNAL);
    } else {
        memset(buffer, 0xFF, sizeof(buffer));
        spiffs_page_header ph = {.obj_id = (spiffs_obj_id)(o->obj | SR_IX), .span_ix = (spiffs_span_ix)j,
                                 .flags = 0xFF & ~SPIFFS_PH_FLAG_INDEX};
        memcpy(buffer, &ph, sizeof(ph));
    }
    uint8_t *table = buffer + table_offset(j);
    for (uint32_t i = 0; i < entries; ++i) put16(table + i * 2, first + i < o->spans ? target[first + i] : SR_NO_PAGE);
    /* A torn size goes: the copy holds the size the pages hold (an empty
     * file keeps SPIFFS's 0xFFFFFFFF, so its first append still programs the
     * header in place). */
    if (j == 0 && o->size_recovered)
        put32(buffer + offsetof(spiffs_page_object_ix_header, size), o->size ? o->size : SPIFFS_UNDEFINED_LEN);
    spiffs_page_ix pix;
    s32_t res;
    r->wrote = true;
    if (old != SR_NO_PAGE) {
        /* SPIFFS's own page move: the copy is written, given its lookup entry
         * and finalized before the old page is deleted. A reset in between
         * leaves two copies; the next survey keeps the one whose references
         * are perfect. */
        res = spiffs_page_move(r->fs, 0, buffer, (spiffs_obj_id)(o->obj | SR_IX), 0, old, &pix);
    } else {
        spiffs_page_header ph;
        memcpy(&ph, buffer, sizeof(ph));
        res = spiffs_page_allocate_data(r->fs, (spiffs_obj_id)(o->obj | SR_IX), &ph, buffer + sizeof(ph),
                                        r->page_size - sizeof(ph), 0, 1, &pix);
        if (res == SPIFFS_OK) r->report->index_created++;
    }
    if (res != SPIFFS_OK) return fail(r, old != SR_NO_PAGE ? "index_move" : "index_create", res);
    /* The flags byte differs from the RAM copy only by finalization. */
    if (!verify_page(r, pix, buffer, 0, 4) ||
        !verify_page(r, pix, buffer, sizeof(spiffs_page_header), r->page_size - sizeof(spiffs_page_header)))
        return fail(r, "index_verify", SPIFFS_ERR_INTERNAL);
    o->ix[j] = pix;
    r->report->index_rewritten++;
    if (j == 0 && o->size_recovered) {
        o->size_recovered = false;
        o->actions |= SRR_FILE_SIZE_RECOVERED;
        r->report->sizes_recovered++;
        char message[160];
        snprintf(message, sizeof(message), "%s: torn header size %lu replaced by %lu, where its pages end", o->name,
                 (unsigned long)o->header_size, (unsigned long)o->size);
        logf_line(r, "WARN", "STORAGE_REPAIR_SIZE_RECOVERED", message);
    }
    return true;
}

static bool normalize_object(run_t *r, obj_t *o)
{
    if (!o->needs_work) return true;
    for (uint32_t i = 0; i < o->drops; ++i) {
        if (!delete_page(r, o->drop[i])) return fail(r, "drop_page", SPIFFS_ERR_INTERNAL);
        pace(r);
    }
    r->report->strays_removed += o->strays;
    r->report->duplicates_removed += o->drops - o->strays;
    o->drops = o->strays = 0;
    uint16_t *target = zalloc(r, (o->spans ? o->spans : 1) * sizeof(uint16_t));
    if (!target) return fail(r, "normalize_memory", SPIFFS_ERR_INTERNAL);
    for (uint32_t k = 0; k < o->spans; ++k) target[k] = o->ref[k];
    bool ok = true;
    for (uint32_t i = 0; ok && i < o->patches; ++i) {
        patch_t *p = &o->patch[i];
        if (!going(r)) {
            ok = false;
            break;
        }
        if (p->kind == PATCH_REFER) target[p->span] = p->refer;
        else {
            uint16_t pix;
            ok = new_data_page(r, o, p->span, p->data, p->length, &pix);
            if (ok) target[p->span] = pix;
        }
        if (ok) {
            o->rewritten++;
            r->report->spans_rewritten++;
        }
    }
    for (uint32_t j = 0; ok && j < o->ix_spans; ++j)
        if (o->ix_rewrite[j]) ok = rewrite_index(r, o, j, target);
    release(r, target);
    if (ok) o->actions |= SRR_FILE_NORMALIZED;
    return ok;
}

/* ---- the check ------------------------------------------------------ */
void srr_check_callback(spiffs *fs, spiffs_check_type type, spiffs_check_report report, u32_t arg1, u32_t arg2)
{
    (void)fs;
    (void)type;
    (void)arg2;
    run_t *r = s_run;
    if (!r) return;
    /* Never a SPIFFS call here: the check holds the filesystem lock. */
    switch (report) {
    case SPIFFS_CHECK_PROGRESS:
        if (r->ports->yield) r->ports->yield(r->ports->context);
        break;
    case SPIFFS_CHECK_ERROR:
        r->errors++;
        r->last_error = (int32_t)arg1;
        break;
    case SPIFFS_CHECK_FIX_INDEX: r->fix_index++; break;
    case SPIFFS_CHECK_FIX_LOOKUP: r->fix_lookup++; break;
    case SPIFFS_CHECK_DELETE_ORPHANED_INDEX: r->del_orphan++; break;
    case SPIFFS_CHECK_DELETE_PAGE: r->del_page++; break;
    case SPIFFS_CHECK_DELETE_BAD_FILE:
        r->del_bad_file++;
        if (r->deleted_count < sizeof(r->deleted_ids) / sizeof(r->deleted_ids[0]))
            r->deleted_ids[r->deleted_count++] = (uint16_t)(arg1 & ~SR_IX);
        break;
    default: break;
    }
}

/* One SPIFFS_check pass. SPIFFS_check returns only its final scan's result;
 * a failed lookup, index or page pass reaches only the callback. */
static bool check_pass(run_t *r, uint32_t pass, bool *fixed)
{
    uint32_t before = r->fix_index + r->fix_lookup + r->del_orphan + r->del_page + r->del_bad_file;
    uint32_t errors = r->errors;
    /* The check of a consistent store writes only what it fixes (which
     * then counts as a repair): no repair LED for it. */
    set_phase(r, SRP_PHASE_CHECK, pass, false);
    uint64_t started = r->ports->now_ms ? r->ports->now_ms(r->ports->context) : 0;
    s32_t res = SPIFFS_check(r->fs);
    SPIFFS_clearerr(r->fs);
    if (r->ports->now_ms) {
        r->last_check_ms = (uint32_t)(r->ports->now_ms(r->ports->context) - started);
        r->report->check_ms += r->last_check_ms;
    }
    r->report->check_passes++;
    *fixed = r->fix_index + r->fix_lookup + r->del_orphan + r->del_page + r->del_bad_file != before;
    if (*fixed) r->wrote = true;
    /* A file the check deleted that the survey saw as live is a finding:
     * removals in progress (hidden headers) are expected to go. */
    for (uint32_t i = 0; i < r->deleted_count; ++i)
        for (uint32_t k = 0; k < r->obj_count; ++k)
            if (live(&r->objs[k]) && r->objs[k].obj == r->deleted_ids[i]) {
                r->report->check_deleted_live++;
                char message[120];
                snprintf(message, sizeof(message), "the check deleted %s", r->objs[k].name);
                logf_line(r, "ERROR", "STORAGE_REPAIR_CHECK_DELETED", message);
            }
    r->deleted_count = 0;
    if (res != SPIFFS_OK && r->errors == errors) {
        r->errors++;
        r->last_error = res;
    }
    char message[200];
    snprintf(message, sizeof(message),
             "pass %lu: fix_index %lu fix_lookup %lu delete_orphan_index %lu delete_page %lu delete_bad_file %lu "
             "errors %lu last %ld",
             (unsigned long)pass, (unsigned long)r->fix_index, (unsigned long)r->fix_lookup,
             (unsigned long)r->del_orphan, (unsigned long)r->del_page, (unsigned long)r->del_bad_file,
             (unsigned long)r->errors, (long)r->last_error);
    logf_line(r, r->errors != errors || r->del_bad_file ? "WARN" : "INFO", "STORAGE_REPAIR_CHECKED", message);
    return r->errors == errors;
}

/* ---- removal -------------------------------------------------------- */
static bool remove_name(run_t *r, const char *name)
{
    r->wrote = true;
    s32_t res = SPIFFS_remove(r->fs, name);
    SPIFFS_clearerr(r->fs);
    return res == SPIFFS_OK;
}

/* An expendable file whose header has a torn size: the first append to a
 * new file rewrites its header in place, and a reset there leaves a size
 * like 0xFFFFFFFB. SPIFFS can neither truncate nor remove such a file, the
 * check keeps it, and every later create of that name fails (13FL's and
 * P02's probe ENOENT). Its pages go by object id. Needs a fresh survey. */
static bool purge_object(run_t *r, const obj_t *o)
{
    for (uint32_t k = 0; k < r->obj_count; ++k)
        if (&r->objs[k] != o && r->objs[k].obj == o->obj && !r->objs[k].hidden) return false;
    for (uint32_t pix = 0; pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        if (pix % r->ppb == 0 || pg->lu == SPIFFS_OBJ_ID_FREE || pg->lu == SPIFFS_OBJ_ID_DELETED ||
            (uint16_t)(pg->lu & 0x7FFFU) != o->obj)
            continue;
        if (!delete_page(r, (uint16_t)pix)) return false;
        pace(r);
    }
    return true;
}

static bool rename_rebuild(run_t *r, obj_t *rebuilt, const char *temp, const char *name);
static bool ensure_room(run_t *r);
static uint32_t compact(run_t *r, uint32_t target, bool *touched);
static int32_t counted_free(const run_t *r);

/* Probe leftovers and the repair's own leftover rebuilds (once no manifest
 * names their files) always; transient cursor stages only when the store
 * needs room. */
static void remove_expendables(run_t *r, bool for_room)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        bool rebuild = restore_file(o->name);
        if (rebuild && manifest_active(r)) continue;
        if (!o->hidden && o->header != SR_NO_PAGE && !o->plausible && o->name[0] &&
            o->kind.cls == SRP_CLASS_EXPENDABLE) {
            if (purge_object(r, o)) {
                r->report->expendables_removed++;
                o->actions |= SRR_FILE_REMOVED;
                note_file(r, o);
            }
            continue;
        }
        if (!live(o) || o->kind.cls != SRP_CLASS_EXPENDABLE) continue;
        if (!strstr(o->name, "probe") && !rebuild && !for_room) continue;
        if (rebuild) {
            /* Custody bytes the repair wrote: when the file they were for
             * is missing, they go into place rather than away. */
            char name[SRP_NAME_BYTES];
            size_t length = strlen(o->name) - (sizeof(SRP_RESTORE_SUFFIX) - 1U);
            memcpy(name, o->name, length);
            name[length] = '\0';
            if (!find_live(r, name) && rename_rebuild(r, o, o->name, name)) {
                char message[160];
                snprintf(message, sizeof(message), "%s: a leftover rebuild went into place", name);
                logf_line(r, "WARN", "STORAGE_REPAIR_REBUILT", message);
                continue;
            }
            r->failed = false;
        }
        if (remove_name(r, o->name)) {
            r->report->expendables_removed++;
            o->actions |= SRR_FILE_REMOVED;
            note_file(r, o);
        }
    }
}

static bool remove_expendables_now(run_t *r)
{
    remove_expendables(r, true);
    return true;
}

static const char *const CATALOG_FILES[] = {"/add_identities.enc", "/add_identities.tmp", "/add_identities.stage",
                                            "/add_identities.backup", "/add_identities.commit"};

static int catalog_load(void *context, ft_checkpoint_t *cp)
{
    return nvs_load(context, "file_tx", "catalog", cp, sizeof(*cp));
}
static bool catalog_commit(void *context, const ft_checkpoint_t *cp)
{
    return nvs_store(context, "file_tx", "catalog", cp, sizeof(*cp));
}

/* The identity catalog is re-fetchable: ADD sends it on every connect. Its
 * files go first and then its transaction is set idle, so ft_recover finds
 * nothing to finish; a reset in between repeats both on the next boot. */
static bool clear_catalog(run_t *r, bool for_room)
{
    bool any = false;
    for (size_t i = 0; i < sizeof(CATALOG_FILES) / sizeof(CATALOG_FILES[0]); ++i) any |= find_live(r, CATALOG_FILES[i]) != NULL;
    /* Making room with no catalog file: nothing to drop, nothing to write. */
    if (for_room && !any && !(r->state.flags & SRP_STATE_CATALOG_CLEAR)) return true;
    r->state.flags |= SRP_STATE_CATALOG_CLEAR;
    if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    bool removed = true;
    uint32_t dropped = 0;
    for (size_t i = 0; i < sizeof(CATALOG_FILES) / sizeof(CATALOG_FILES[0]); ++i) {
        obj_t *o = find_live(r, CATALOG_FILES[i]);
        if (!o) continue;
        if (!remove_name(r, CATALOG_FILES[i])) {
            removed = false;
            continue;
        }
        dropped++;
        o->actions |= SRR_FILE_REMOVED;
        note_file(r, o);
    }
    if (!removed) {
        /* A file SPIFFS would not remove (not consistent yet): the flag
         * stays and the next run repeats this; the transaction is not set
         * idle while a catalog file remains. */
        logf_line(r, "WARN", "STORAGE_REPAIR_CATALOG_DEFERRED", "a catalog file could not be removed yet");
        return true;
    }
    ft_port_t port = {catalog_load, catalog_commit, r};
    if (!ft_checkpoint_idle(port)) return fail(r, "catalog_idle", SPIFFS_ERR_INTERNAL);
    r->state.flags &= ~SRP_STATE_CATALOG_CLEAR;
    if (dropped)
        logf_line(r, "WARN", "STORAGE_REPAIR_CATALOG_DROPPED", "identity catalog removed; ADD resends it on connect");
    return save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL);
}

/* ---- legacy queue cursors ------------------------------------------- */
typedef struct {
    run_t *r;
    obj_t *o;
} byte_source_t;

static bool source_read(void *context, uint32_t offset, uint8_t *buffer, size_t length)
{
    byte_source_t *s = context;
    return object_read(s->r, s->o, offset, buffer, (uint32_t)length);
}

static const struct {
    const char *name, *ns, *key;
} QUEUES[] = {
    {"/pending.jsonl", "legacy_queues", "ords_pending"},
    {"/blocked_identity.jsonl", "legacy_queues", "blocked"},
    {"/corrupt_ords.jsonl", "legacy_queues", "old_qo"},
    {"/add_corrupt.jsonl", "legacy_queues", "old_qa"},
    {"/add_corrupt.bak", "legacy_queues", "old_qb"},
    {"/add_live.jsonl", "add_legacy", "live"},
    {"/add_pending.jsonl", "add_legacy", "reconcile"},
};

/* A cursor lq_open_step would reject (DQ_CORRUPT wedges the production
 * drains and latches LOCAL_FAILURE), or one past a file an older image
 * rewrote, is cleared: every retained row replays, which ORDS (409) and ADD
 * (event_uid) settle once. The old cursor goes to ADD as evidence. With
 * remove_settled, a fully settled file is retired as lq_reclaim retires it:
 * its hold and manifest entry go first, so neither a restore in this run nor
 * a resume after a reset brings it back. */
static bool reconcile_cursor(run_t *r, size_t q, bool remove_settled)
{
    lq_checkpoint_t cp;
    memset(&cp, 0, sizeof(cp));
    int loaded = nvs_load(r, QUEUES[q].ns, QUEUES[q].key, &cp, sizeof(cp));
    if (loaded < 0) return true; /* unreadable NVS: never overwrite */
    obj_t *o = find_live(r, QUEUES[q].name);
    byte_source_t source = {r, o};
    srp_bytes_t bytes = {&source, source_read};
    srp_cursor_verdict_t verdict = srp_cursor_verdict(loaded, &cp, o != NULL, o ? o->size : 0, false,
                                                      o ? &bytes : NULL);
    if (verdict == SRP_CURSOR_PREFIX_GAP) return true; /* unnormalized: the next run decides */
    if (verdict == SRP_CURSOR_KEEP) {
        if (remove_settled && o && loaded == 1 && cp.offset == o->size && o->size) {
            lq_checkpoint_t next;
            if (!lq_checkpoint_cleared(&cp, &next) || !nvs_store(r, QUEUES[q].ns, QUEUES[q].key, &next, sizeof(next)))
                return fail(r, "cursor_commit", SPIFFS_ERR_INTERNAL);
            if (!manifest_mark(r, o->name, SRP_MANIFEST_SETTLED)) return false;
            drop_hold(r, o);
            o->retired = true;
            if (remove_name(r, QUEUES[q].name)) {
                r->report->settled_removed++;
                o->actions |= SRR_FILE_REMOVED;
                note_file(r, o);
            }
        }
        return true;
    }
    lq_checkpoint_t base = cp, next;
    if (verdict == SRP_CURSOR_CORRUPT) base.generation = 0;
    if (!lq_checkpoint_cleared(&base, &next)) return fail(r, "cursor_generation", SPIFFS_ERR_INTERNAL);
    srp_reset_t reset;
    memset(&reset, 0, sizeof(reset));
    snprintf(reset.name, sizeof(reset.name), "%s", QUEUES[q].name);
    reset.verdict = (uint8_t)verdict;
    reset.generation = r->state.generation;
    reset.previous = cp;
    /* With every reset record unacknowledged the old cursor's evidence is
     * dropped (counted, logged), but the cursor is still cleared: a cursor
     * lq_open_step rejects would wedge the drain, and replaying costs no row. */
    bool kept = srp_resets_add(r->resets, &reset);
    if (!kept) r->report->records_dropped++;
    if (!save_records(r) || !nvs_store(r, QUEUES[q].ns, QUEUES[q].key, &next, sizeof(next)))
        return fail(r, "cursor_commit", SPIFFS_ERR_INTERNAL);
    r->report->cursors_reset++;
    if (o) {
        o->actions |= SRR_FILE_CURSOR_RESET;
        note_file(r, o);
    }
    char message[200];
    snprintf(message, sizeof(message), "%s: cursor %lu (generation %lu) cleared, %s; every retained row replays%s",
             QUEUES[q].name, (unsigned long)cp.offset, (unsigned long)cp.generation, srp_cursor_verdict_name(verdict),
             kept ? "" : " (its record dropped: every reset record unacknowledged)");
    logf_line(r, "WARN", "STORAGE_REPAIR_CURSOR_RESET", message);
    return true;
}

static bool reconcile_cursors(run_t *r, bool remove_settled)
{
    for (size_t q = 0; q < sizeof(QUEUES) / sizeof(QUEUES[0]); ++q)
        if (!reconcile_cursor(r, q, remove_settled)) return false;
    return true;
}

/* ---- tails ---------------------------------------------------------- */
static uint32_t settled_offset(run_t *r, const obj_t *o)
{
    lq_checkpoint_t cp;
    if (!o->kind.lq_namespace ||
        nvs_load(r, o->kind.lq_namespace, o->kind.lq_key, &cp, sizeof(cp)) != 1 || !lq_checkpoint_valid(&cp))
        return 0;
    return cp.offset;
}

/* rel_open_append refuses a file whose last byte is not '\n' (EBADMSG), so a
 * torn append blocks every later one. One '\n' makes the fragment a complete
 * malformed row that the drain hands to ADD. A torn command line is left to
 * add_connector's merge of the command inbox. */
static bool hidden_pinned(run_t *r)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        const obj_t *o = &r->objs[i];
        srp_manifest_file_t *f = o->hidden && o->header != SR_NO_PAGE ? manifest_entry(r, o->name) : NULL;
        if (f && f->object_id == o->obj && !(f->flags & SRP_MANIFEST_SETTLED)) return true;
    }
    return false;
}

static bool terminate_tails(run_t *r)
{
    /* A SPIFFS append with three free blocks or fewer runs SPIFFS's GC,
     * which wipes the data pages of a hidden original an unsettled manifest
     * entry still waits for: the tails wait for a boot with room. */
    if ((uint32_t)r->fs->free_blocks <= 3 && hidden_pinned(r)) return true;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || !o->size || !o->consistent) continue;
        bool command = (o->kind.flags & SRP_COMMAND_FAMILY) != 0;
        if (!(o->kind.flags & SRP_APPEND_TARGET) && !command) continue;
        uint8_t last;
        if (!object_read(r, o, o->size - 1, &last, 1) || last == '\n') continue;
        if (command) {
            r->report->commands_damaged = true;
            if (!(r->state.flags & SRP_STATE_COMMANDS_DAMAGED)) {
                r->state.flags |= SRP_STATE_COMMANDS_DAMAGED;
                if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
            }
            continue;
        }
        if (settled_offset(r, o) >= o->size) continue; /* settled: the drain retires it */
        r->wrote = true;
        spiffs_file fd = SPIFFS_open(r->fs, o->name, SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
        bool ok = fd >= 0 && SPIFFS_write(r->fs, fd, (void *)"\n", 1) == 1 && SPIFFS_fflush(r->fs, fd) >= 0;
        if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
        SPIFFS_clearerr(r->fs);
        if (!ok) return fail(r, "tail_append", SPIFFS_ERR_INTERNAL);
        o->actions |= SRR_FILE_TAIL;
        r->report->tails_terminated++;
        char message[120];
        snprintf(message, sizeof(message), "%s: torn row at %lu terminated", o->name, (unsigned long)o->size);
        logf_line(r, "WARN", "STORAGE_REPAIR_TAIL_TERMINATED", message);
        o->size += 1;
        note_file(r, o);
    }
    return true;
}

/* ---- restore -------------------------------------------------------- */
static bool spiffs_append(run_t *r, const char *name, obj_t *from, uint32_t offset, uint32_t end, uint32_t *done)
{
    uint8_t *chunk = zalloc(r, SR_COPY_BYTES);
    if (!chunk) return false;
    r->wrote = true;
    spiffs_file fd = SPIFFS_open(r->fs, name, SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
    bool ok = fd >= 0;
    /* Whole data pages per write: SPIFFS writes them past its cache, so
     * every byte before `offset` is on flash once its write returned. */
    while (ok && offset < end) {
        uint32_t n = end - offset < SR_COPY_BYTES ? end - offset : SR_COPY_BYTES;
        ok = held_read(from, offset, chunk, n) && SPIFFS_write(r->fs, fd, chunk, (s32_t)n) == (s32_t)n;
        if (ok) {
            offset += n;
            *done += n;
        }
        pace(r);
    }
    if (ok) ok = SPIFFS_fflush(r->fs, fd) >= 0;
    if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    SPIFFS_clearerr(r->fs);
    release(r, chunk);
    return ok;
}

/* Records held bytes of a file that did not go back, before the hold dies
 * with this boot. False when the record could not be kept (a full gap
 * list): the caller then leaves the manifest entry unreported, and the
 * manifest itself keeps the obligation until a later boot can record it. */
static bool record_unrestored_flags(run_t *r, obj_t *o, const char *name, uint32_t offset, uint32_t end,
                                    int32_t code, srp_gap_cause_t cause, uint8_t flags)
{
    r->restore_failed = true;
    if (end <= offset) return true;
    obj_t target = *o;
    snprintf(target.name, sizeof(target.name), "%s", name);
    bool recorded = record_gap_flags(r, &target, offset, end - offset, code, cause, flags) && save_records(r);
    if (recorded) {
        o->gaps = target.gaps;
        o->gap_bytes = target.gap_bytes;
        o->actions |= target.actions;
    }
    note_file(r, o);
    return recorded;
}

static bool record_unrestored(run_t *r, obj_t *o, const char *name, uint32_t offset, uint32_t end, int32_t code,
                              srp_gap_cause_t cause)
{
    return record_unrestored_flags(r, o, name, offset, end, code, cause, 0);
}

/* SPIFFS_rename writes one page (the header's page move). On a store too
 * full for SPIFFS's own allocation that page comes from the GC reserve. */
static s32_t rename_into_place(run_t *r, const char *temp, const char *name)
{
    r->wrote = true;
    s32_t res = SPIFFS_rename(r->fs, temp, name);
    SPIFFS_clearerr(r->fs);
    if (res == SPIFFS_ERR_FULL) {
        r->fs->cleaning = 1;
        res = SPIFFS_rename(r->fs, temp, name);
        r->fs->cleaning = 0;
        SPIFFS_clearerr(r->fs);
    }
    return res;
}

/* Rebuilds a held file the check removed: the held bytes go to <name>.rpr,
 * are read back, and the file is renamed into place, so a reset never leaves
 * a partial file under the real name and nothing live is ever removed for
 * it. The manifest entry names the rebuild (RESTORING, its object id) before
 * a byte is written, and a partial rebuild's missing tail is recorded before
 * it takes the name: the next boot never takes it for a new file of the
 * owner's. True when the whole file is back. */
static bool rebuild_from_hold(run_t *r, obj_t *o, const char *name, srp_manifest_file_t *f)
{
    char temp[SRP_NAME_BYTES];
    if (!o->hold || !restore_name(name, temp)) return false;
    /* No survey here (it would move o's hold): a torn leftover was purged
     * by the resume before anything else; a consistent one is removed. */
    r->wrote = true;
    (void)SPIFFS_remove(r->fs, temp);
    SPIFFS_clearerr(r->fs);
    spiffs_file fd = SPIFFS_open(r->fs, temp, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    spiffs_stat st;
    bool created = fd >= 0 && SPIFFS_fstat(r->fs, fd, &st) == SPIFFS_OK;
    if (fd >= 0) SPIFFS_close(r->fs, fd);
    SPIFFS_clearerr(r->fs);
    if (!created) {
        (void)record_unrestored(r, o, name, 0, o->hold_size, SPIFFS_ERR_FULL, SRP_GAP_CHECK_CHANGED);
        return false;
    }
    if (f) {
        f->flags = (uint8_t)((f->flags | SRP_MANIFEST_RESTORING) & ~(SRP_MANIFEST_MERGING | SRP_MANIFEST_ADOPTED));
        f->rebuild_id = (uint16_t)(st.obj_id & ~SR_IX);
        f->restored = 0;
        if (!manifest_save(r)) {
            r->restore_failed = true;
            return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
        }
    }
    uint32_t written = 0;
    bool whole = spiffs_append(r, temp, o, 0, o->hold_size, &written);
    /* Read back what went to flash before it takes the name. */
    uint8_t *chunk = zalloc(r, SR_COPY_BYTES), *held = zalloc(r, SR_COPY_BYTES);
    uint32_t verified = 0;
    fd = chunk && held ? SPIFFS_open(r->fs, temp, SPIFFS_O_RDONLY, 0) : -1;
    while (fd >= 0 && verified < written) {
        uint32_t n = written - verified < SR_COPY_BYTES ? written - verified : SR_COPY_BYTES;
        if (SPIFFS_read(r->fs, fd, chunk, (s32_t)n) != (s32_t)n || !held_read(o, verified, held, n) ||
            memcmp(chunk, held, n))
            break;
        verified += n;
        pace(r);
    }
    if (fd >= 0) SPIFFS_close(r->fs, fd);
    SPIFFS_clearerr(r->fs);
    release(r, chunk);
    release(r, held);
    whole = whole && verified == o->hold_size;
    if (!whole) {
        /* The missing tail is a record before the partial file takes the
         * name; the manifest keeps where the rebuild ends. */
        bool recorded = record_unrestored(r, o, name, verified, o->hold_size, SPIFFS_ERR_FULL, SRP_GAP_CHECK_CHANGED);
        if (f) {
            f->restored = verified;
            if (recorded) f->flags |= SRP_MANIFEST_REPORTED;
            if (!manifest_save(r)) {
                r->restore_failed = true;
                return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
            }
        }
    }
    if (!verified) {
        (void)SPIFFS_remove(r->fs, temp);
        SPIFFS_clearerr(r->fs);
        return false;
    }
    s32_t res = rename_into_place(r, temp, name);
    if (res != SPIFFS_OK) {
        /* The rebuild stays under its temporary name (the manifest stays
         * too): the next boot's resume renames it into place. */
        r->restore_failed = true;
        return false;
    }
    return whole;
}

/* A file the check lazily deleted (SPIFFS_check's DELETE_BAD_FILE clears the
 * header's IXDELE flag and frees nothing itself) keeps every page until a
 * later pass, the next check or SPIFFS's GC frees them; SPIFFS's GC wipes the
 * data pages of an object whose header it cannot find, so any SPIFFS write
 * could destroy the original. A copy of the header with the flag set again
 * (one page, SPIFFS's own page move) makes it a file again: under its own
 * name, or (`name`) under the temporary one while a new file has its name.
 * A reset in between leaves the hidden header beside the live copy, which the
 * survey drops as a stray, or the hidden header alone, which the next boot
 * brings back again. */
static bool unhide_as(run_t *r, const obj_t *hidden, const char *name)
{
    uint8_t buffer[256], expect[256];
    if (r->page_size > sizeof(buffer) || hidden->header == SR_NO_PAGE) return false;
    if (!hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, hidden->header), expect, r->page_size))
        return fail(r, "unhide_read", SPIFFS_ERR_INTERNAL);
    expect[offsetof(spiffs_page_header, flags)] |= SPIFFS_PH_FLAG_IXDELE;
    if (name) {
        memset(expect + offsetof(spiffs_page_object_ix_header, name), 0, SPIFFS_OBJ_NAME_LEN);
        memcpy(expect + offsetof(spiffs_page_object_ix_header, name), name, strlen(name));
    }
    spiffs_page_ix pix = 0;
    s32_t res = SPIFFS_ERR_FULL;
    r->wrote = true;
    for (int reserve = 0; reserve < 2 && res == SPIFFS_ERR_FULL; ++reserve) {
        /* The second try takes the page from the GC reserve: compaction
         * gives it back once the file is whole again. */
        memcpy(buffer, expect, r->page_size);
        r->fs->cleaning = reserve ? 1 : 0;
        res = spiffs_page_move(r->fs, 0, buffer, (spiffs_obj_id)(hidden->obj | SR_IX), 0, hidden->header, &pix);
        r->fs->cleaning = 0;
    }
    SPIFFS_clearerr(r->fs);
    if (res != SPIFFS_OK) return fail(r, res == SPIFFS_ERR_FULL ? "full" : "unhide", res);
    if (!verify_page(r, pix, expect, 0, r->page_size)) return fail(r, "unhide_verify", SPIFFS_ERR_INTERNAL);
    r->report->files_restored++;
    char message[160];
    if (name)
        snprintf(message, sizeof(message), "%s: the check had begun deleting it; its pages wait as %s", hidden->name,
                 name);
    else
        snprintf(message, sizeof(message), "%s: the check had begun deleting it; its pages are its file again",
                 hidden->name);
    logf_line(r, "WARN", "STORAGE_REPAIR_UNHIDDEN", message);
    return true;
}

static bool unhide(run_t *r, const obj_t *hidden) { return unhide_as(r, hidden, NULL); }

static bool object_crc(run_t *r, obj_t *o, uint32_t length, uint32_t *crc)
{
    uint8_t span[SRP_SPAN_BYTES];
    uint32_t value = 0;
    for (uint32_t offset = 0; offset < length;) {
        uint32_t n = length - offset < SRP_SPAN_BYTES ? length - offset : SRP_SPAN_BYTES;
        if (!object_read(r, o, offset, span, n)) return false;
        value = srp_crc32(value, span, n);
        offset += n;
    }
    *crc = value;
    return true;
}

static bool matches(run_t *r, obj_t *o, const srp_manifest_file_t *f)
{
    uint32_t crc;
    return o && o->mapped && o->size >= f->size && object_crc(r, o, f->size, &crc) && crc == f->crc;
}

/* What is left of the manifest's file under a hidden header: the same object
 * id and name (SPIFFS gives no other object that id while the header is on
 * flash). *intact: every byte readable and matching. Mapped on demand. */
static obj_t *hidden_remnant(run_t *r, const srp_manifest_file_t *f, bool *intact)
{
    obj_t *found = NULL;
    *intact = false;
    for (uint32_t k = 0; k < r->obj_count; ++k) {
        obj_t *o = &r->objs[k];
        if (!o->hidden || o->header == SR_NO_PAGE || !o->plausible || o->obj != f->object_id || strcmp(o->name, f->name))
            continue;
        if (!o->mapped && (!map_object(r, o) || !fill_gaps(r, o))) {
            if (r->failed) return NULL;
            continue;
        }
        if (o->size == f->size && matches(r, o, f)) {
            *intact = true;
            return o;
        }
        if (!found) found = o;
    }
    return found;
}

/* The manifest's own copy of its file under the temporary name (rebuild_id). */
static obj_t *find_rebuild(run_t *r, const srp_manifest_file_t *f)
{
    char temp[SRP_NAME_BYTES];
    if (!(f->flags & SRP_MANIFEST_RESTORING) || !f->rebuild_id || !restore_name(f->name, temp)) return NULL;
    for (uint32_t k = 0; k < r->obj_count; ++k)
        if (live(&r->objs[k]) && r->objs[k].obj == f->rebuild_id && !strcmp(r->objs[k].name, temp)) return &r->objs[k];
    return NULL;
}

/* Such a copy waits for the resume: no normalization writes it and no record
 * names it (its unreadable bytes are recorded where its rows go). */
static bool frozen(run_t *r, const obj_t *o)
{
    return live(o) && rebuild_entry(r, o->name, o->obj) != NULL;
}

/* Every unreadable run of o (its masked bytes) as a record of `name` at
 * `shift` plus its offset; runs less than a page apart are one merged
 * record, as in record_salvage_gaps. */
static bool record_unreadable(run_t *r, obj_t *o, const char *name, uint32_t shift, uint32_t file_size,
                              srp_gap_cause_t cause)
{
    obj_t target;
    memset(&target, 0, sizeof(target));
    snprintf(target.name, sizeof(target.name), "%s", name);
    target.kind = srp_classify(name);
    target.size = file_size;
    for (uint32_t start = next_masked(o, 0); start < o->size;) {
        uint32_t end = masked_end(o, start);
        bool merged = false;
        for (uint32_t next = next_masked(o, end); next < o->size && next - end < SRP_SPAN_BYTES;
             next = next_masked(o, end)) {
            end = masked_end(o, next);
            merged = true;
        }
        patch_t *p = find_patch(o, start / SRP_SPAN_BYTES);
        if (!record_gap_flags(r, &target, shift + start, end - start, p ? p->code : 0, cause,
                              merged ? SRP_GAP_MERGED : 0))
            return false;
        r->report->interrupted_losses++;
        start = next_masked(o, end);
        pace(r);
    }
    if (target.gaps) note_file(r, &target);
    return true;
}

/* One record of [offset, offset + length) of `name`. */
static bool record_range(run_t *r, const char *name, uint32_t offset, uint32_t length, uint32_t file_size,
                         srp_gap_cause_t cause)
{
    obj_t target;
    memset(&target, 0, sizeof(target));
    snprintf(target.name, sizeof(target.name), "%s", name);
    target.kind = srp_classify(name);
    target.size = file_size;
    if (!length) return true;
    if (!record_gap_flags(r, &target, offset, length, 0, cause, 0)) return false;
    r->report->interrupted_losses++;
    note_file(r, &target);
    return true;
}

/* Bytes [offset, end) of src, read through the survey's map (its own pages:
 * appending to another file moves none of them), appended to `name`. */
static bool copy_append(run_t *r, const char *name, obj_t *src, uint32_t offset, uint32_t end)
{
    uint8_t *chunk = zalloc(r, SR_COPY_BYTES);
    if (!chunk) return false;
    r->wrote = true;
    spiffs_file fd = SPIFFS_open(r->fs, name, SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
    bool ok = fd >= 0;
    while (ok && offset < end) {
        uint32_t n = end - offset < SR_COPY_BYTES ? end - offset : SR_COPY_BYTES;
        ok = object_read(r, src, offset, chunk, n) && SPIFFS_write(r->fs, fd, chunk, (s32_t)n) == (s32_t)n;
        offset += n;
        pace(r);
    }
    if (ok) ok = SPIFFS_fflush(r->fs, fd) >= 0;
    if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    SPIFFS_clearerr(r->fs);
    release(r, chunk);
    return ok;
}

/* 1 when a's bytes at a_offset equal b's at b_offset over length, 0 when
 * not (or unreadable), -1 without memory. */
static int same_bytes(run_t *r, obj_t *a, uint32_t a_offset, obj_t *b, uint32_t b_offset, uint32_t length)
{
    uint8_t *x = zalloc(r, SR_COPY_BYTES), *y = zalloc(r, SR_COPY_BYTES);
    int same = x && y ? 1 : -1;
    for (uint32_t done = 0; same == 1 && done < length;) {
        uint32_t n = length - done < SR_COPY_BYTES ? length - done : SR_COPY_BYTES;
        if (!object_read(r, a, a_offset + done, x, n) || !object_read(r, b, b_offset + done, y, n) || memcmp(x, y, n))
            same = 0;
        done += n;
        pace(r);
    }
    release(r, x);
    release(r, y);
    return same;
}

/* Room to append `bytes` without SPIFFS's GC. spiffs_gc_check cleans a block
 * in every write unless more than three blocks are free (and enough pages),
 * and SPIFFS's GC before normalization, or over a hidden header, is 1.2 item
 * 8's hazard. The free blocks must stay above three through every page the
 * append allocates (its data pages, its index pages and a header move per
 * write), so compaction makes them first, or nothing is written. Needs a
 * fresh survey; surveys again after compacting. */
static bool room_for_append(run_t *r, uint32_t bytes)
{
    uint32_t data = bytes / SRP_SPAN_BYTES + 2;
    uint32_t pages = data + 2 * (data / r->ix_len + 2) + bytes / SR_COPY_BYTES + 4;
    uint32_t target = 5 + (pages + r->entries - 1) / r->entries;
    for (uint32_t round = 0; (uint32_t)r->fs->free_blocks < target && round < 8 && going(r); ++round) {
        bool touched = false;
        uint32_t cleaned = compact(r, target, &touched);
        if (touched && !survey(r)) return false;
        if (!cleaned) break;
    }
    return (uint32_t)r->fs->free_blocks >= target && counted_free(r) > (int32_t)(pages + 2 * r->entries);
}

/* In place: every span whose bytes differ from the hold gets a new page with
 * the held bytes (the file is never removed); held bytes past the file's end
 * are appended. */
static bool restore_in_place(run_t *r, obj_t *o, bool *changed)
{
    uint8_t now_bytes[SRP_SPAN_BYTES], held_bytes[SRP_SPAN_BYTES];
    uint32_t common = o->size < o->hold_size ? o->size : o->hold_size;
    release(r, o->patch);
    o->patch = NULL;
    o->patches = o->patch_cap = 0;
    o->drops = o->strays = 0;
    memset(o->ix_rewrite, 0, o->ix_spans);
    bool differs = false;
    for (uint32_t k = 0; k * SRP_SPAN_BYTES < common; ++k) {
        uint32_t start = k * SRP_SPAN_BYTES, length = span_length(o, k);
        uint32_t held_length = common - start < length ? common - start : length;
        bool readable = o->perfect[k] && object_read(r, o, start, now_bytes, length);
        if (readable && held_read(o, start, held_bytes, held_length) && !memcmp(now_bytes, held_bytes, held_length))
            continue;
        patch_t *p = push_patch(r, o, k);
        if (!p || !held_read(o, start, held_bytes, held_length)) return fail(r, "restore", SPIFFS_ERR_INTERNAL);
        p->length = (uint16_t)length;
        /* The file's own bytes past the hold are unreadable: the caller
         * reports the span. */
        if (held_length < length && !readable) return false;
        if (held_length < length) memcpy(p->data, now_bytes, length);
        memcpy(p->data, held_bytes, held_length);
        p->kind = PATCH_COPY;
        p->source = SOURCE_HELD;
        p->filled = 1;
        o->perfect[k] = 0;
        o->ix_rewrite[SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, k)] = 1;
        differs = true;
    }
    if (differs) {
        *changed = true;
        o->needs_work = true;
        if (!normalize_object(r, o)) return false;
    }
    if (o->size < o->hold_size) {
        *changed = true;
        uint32_t done = 0;
        if (!spiffs_append(r, o->name, o, o->size, o->hold_size, &done)) return false;
    }
    if (*changed) {
        o->actions |= SRR_FILE_RESTORED;
        r->report->files_restored++;
        note_file(r, o);
    }
    return true;
}

/* Puts back what the check changed. First every file the check had only
 * begun deleting, held or not, by its own header (one page each, before any
 * rebuild's SPIFFS writes could run SPIFFS's GC over a hidden header); an
 * unheld one that is no longer whole has its unreadable bytes recorded
 * first. Then held files: in place, or whole under a temporary name when the
 * check removed them. What cannot go back is recorded (CHECK_CHANGED,
 * UNHELD_LOST) and its manifest entry stays (REPORTED once recorded), so the
 * next boot tries again from whatever the store still holds; an entry whose
 * original is still under a hidden header is never settled, and the run
 * fails. */
static bool restore_held(run_t *r, bool *changed)
{
    *changed = false;
    bool unhidden = false;
    for (uint32_t i = 0; manifest_active(r) && i < r->manifest->count; ++i) {
        srp_manifest_file_t *f = &r->manifest->file[i];
        if (f->flags & (SRP_MANIFEST_SETTLED | SRP_MANIFEST_REPORTED)) continue;
        r->failed = false;
        if (find_live(r, f->name)) continue;
        bool intact = false, held = false;
        obj_t *hidden = hidden_remnant(r, f, &intact);
        if (r->failed) return false;
        if (!hidden) continue;
        for (uint32_t k = 0; k < r->obj_count && !held; ++k)
            held = r->objs[k].hold && !strcmp(r->objs[k].name, f->name);
        if (!intact && held) continue; /* its hold has every byte: rebuilt below */
        *changed = true;
        if (!intact) {
            /* Unheld, and pages of it gone: what is left comes back, the
             * rest is a record first. */
            if (!record_unreadable(r, hidden, f->name, 0, f->size, SRP_GAP_UNHELD_LOST) ||
                (hidden->size < f->size &&
                 !record_range(r, f->name, hidden->size, f->size - hidden->size, f->size, SRP_GAP_UNHELD_LOST)) ||
                !save_records(r)) {
                r->restore_failed = true;
                continue;
            }
            f->flags |= SRP_MANIFEST_REPORTED;
            if (!manifest_save(r)) return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
        }
        if (unhide(r, hidden)) {
            unhidden = true;
            if (!intact) {
                f->flags |= SRP_MANIFEST_SETTLED;
                if (!manifest_save(r)) return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
            }
            continue;
        }
        r->failed = false;
        /* No page for its header: a held file is rebuilt below; an unheld
         * one stays hidden and unsettled (below). */
    }
    if (unhidden && !survey(r)) return false;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!o->held || !o->hold || o->retired) continue;
        r->failed = false;
        srp_manifest_file_t *f = manifest_entry(r, o->name);
        if (!live(o)) {
            *changed = true;
            if (rebuild_from_hold(r, o, o->name, f)) {
                o->actions |= SRR_FILE_REBUILT;
                r->report->files_rebuilt++;
                note_file(r, o);
            }
            continue;
        }
        bool one = false;
        if (!restore_in_place(r, o, &one)) {
            /* Every span the check left different, whole: which of the
             * index rewrites landed is unknown. One record over all of
             * them (flagged merged when they are apart), so the gap list's
             * room for one record per manifest entry holds. */
            int32_t code = r->report->error_code ? r->report->error_code : SPIFFS_ERR_INTERNAL;
            uint32_t low = UINT32_MAX, high = 0, ranges = 0;
            for (uint32_t k = 0; k < o->patches; ++k) {
                uint32_t start = o->patch[k].span * SRP_SPAN_BYTES, end = start + o->patch[k].length;
                if (start < low) low = start;
                if (end > high) high = end;
                ranges++;
            }
            if (o->size < o->hold_size) {
                if (o->size < low) low = o->size;
                if (o->hold_size > high) high = o->hold_size;
                ranges++;
            }
            bool recorded = !ranges || record_unrestored_flags(r, o, o->name, low, high, code, SRP_GAP_CHECK_CHANGED,
                                                               ranges > 1 ? SRP_GAP_MERGED : 0);
            r->restore_failed = true;
            if (recorded) (void)manifest_mark(r, o->name, SRP_MANIFEST_REPORTED);
            r->failed = false;
            *changed = true;
            continue;
        }
        *changed |= one;
    }
    /* Custody the budget could not hold. */
    for (uint32_t i = 0; manifest_active(r) && i < r->manifest->count; ++i) {
        srp_manifest_file_t *f = &r->manifest->file[i];
        if (f->flags & (SRP_MANIFEST_SETTLED | SRP_MANIFEST_REPORTED)) continue;
        bool held = false;
        for (uint32_t k = 0; k < r->obj_count && !held; ++k)
            held = r->objs[k].hold && !strcmp(r->objs[k].name, f->name);
        if (held) continue;
        r->failed = false;
        obj_t *now = find_live(r, f->name);
        if (matches(r, now, f)) continue;
        *changed = true;
        bool intact = false;
        if (!now && hidden_remnant(r, f, &intact)) {
            /* Its header could not come back (no page): it is on flash, so
             * the entry stays unsettled and the run fails; the next boot's
             * resume brings it back. */
            r->restore_failed = true;
            r->failed = false;
            char message[160];
            snprintf(message, sizeof(message), "%s: still under a hidden header; kept for the next boot", f->name);
            logf_line(r, "WARN", "STORAGE_REPAIR_RESTORE_DEFERRED", message);
            continue;
        }
        if (r->failed) return false;
        obj_t placeholder;
        memset(&placeholder, 0, sizeof(placeholder));
        memcpy(placeholder.name, f->name, sizeof(placeholder.name));
        placeholder.kind = srp_classify(f->name);
        placeholder.size = f->size;
        obj_t *target = now ? now : &placeholder;
        bool failed_before = r->restore_failed;
        if (record_unrestored(r, target, f->name, 0, f->size, 0, SRP_GAP_UNHELD_LOST)) {
            /* Nothing held to put back and nothing of it on flash: recorded
             * is settled. */
            f->flags |= SRP_MANIFEST_REPORTED | SRP_MANIFEST_SETTLED;
            if (!manifest_save(r)) return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
            r->restore_failed = failed_before;
        }
    }
    if (r->restore_failed) fail(r, "restore", SPIFFS_ERR_INTERNAL);
    return true;
}

/* ---- manifest (resume) ---------------------------------------------- */
/* Every live custody file, held or not, in class priority, with its size and
 * CRC. No check runs (`fits` false, the reason in error_stage) when they do
 * not all fit, or when the gap list could not take one record for each: a
 * file left out would have no resume after a reset in the check, and an entry
 * whose record cannot be kept would settle with nothing reported. */
static bool write_manifest(run_t *r, bool *fits)
{
    srp_manifest_t *m = r->manifest;
    *fits = false;
    memset(m, 0, sizeof(*m));
    m->generation = r->state.generation;
    m->image_tag = r->cfg->image_tag;
    m->flags = r->cfg->pending_verify ? SRP_MANIFEST_PENDING_IMAGE : 0;
    m->rollback_tag = r->cfg->pending_verify ? r->cfg->rollback_tag : 0;
    uint32_t files = 0;
    for (unsigned priority = 0; priority <= 6; ++priority) {
        for (uint32_t i = 0; i < r->obj_count; ++i) {
            obj_t *o = &r->objs[i];
            if (!live(o) || !custody(o) || o->retired || srp_class_priority(o->kind.cls) != priority) continue;
            if (++files > SRP_MANIFEST_FILES) continue;
            srp_manifest_file_t *f = &m->file[m->count++];
            memcpy(f->name, o->name, sizeof(f->name));
            f->cls = (uint8_t)o->kind.cls;
            f->object_id = o->obj;
            f->size = o->size;
            if (o->held) f->crc = o->crc;
            else if (!object_crc(r, o, o->size, &f->crc)) return fail(r, "manifest_read", SPIFFS_ERR_INTERNAL);
            if (cursor_generation(r, &o->kind, &f->cursor_generation)) f->flags |= SRP_MANIFEST_CURSOR;
            pace(r);
        }
    }
    char message[160];
    uint32_t room = SRP_MAX_GAPS - srp_gaps_unacked(r->gaps);
    if (files > SRP_MANIFEST_FILES || files > room) {
        bool many = files > SRP_MANIFEST_FILES;
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), many ? "manifest" : "records");
        snprintf(message, sizeof(message),
                 many ? "%lu custody files, the manifest holds %lu; no check this boot"
                      : "%lu custody files, room for %lu gap records until ADD acknowledges some; no check this boot",
                 (unsigned long)files, (unsigned long)(many ? SRP_MANIFEST_FILES : room));
        logf_line(r, "WARN", many ? "STORAGE_REPAIR_MANIFEST_FULL" : "STORAGE_REPAIR_RECORDS_FULL", message);
        memset(m, 0, sizeof(*m));
        return true;
    }
    if (!manifest_save(r)) return false;
    r->state.flags |= SRP_STATE_MANIFEST;
    r->state.phase = SRP_PHASE_CHECK;
    r->report->manifest_written = true;
    *fits = true;
    return save_state(r);
}

static void supersede(run_t *r, const srp_manifest_file_t *f, obj_t *now, const char *why)
{
    char message[180];
    snprintf(message, sizeof(message), "%s: replaced by its owner after the interrupted run (%s); not a gap", f->name,
             why);
    logf_line(r, "WARN", "STORAGE_REPAIR_SUPERSEDED", message);
    r->report->files_superseded++;
    if (now) {
        now->actions |= SRR_FILE_SUPERSEDED;
        note_file(r, now);
    }
}

/* Renames a whole rebuild into place. SPIFFS_rename takes the first header
 * of that name it meets, so a duplicate header (a reset during the rebuild's
 * last header update leaves the old and the new one) goes first. */
static bool rename_rebuild(run_t *r, obj_t *rebuilt, const char *temp, const char *name)
{
    if (rebuilt->needs_work) {
        /* Its unreadable bytes, if any, lie in the range already recorded. */
        if (!fill_gaps(r, rebuilt)) return fail(r, "fill_memory", SPIFFS_ERR_INTERNAL);
        r->fs->cleaning = 1;
        bool ok = normalize_object(r, rebuilt);
        r->fs->cleaning = 0;
        if (!ok) return false;
    }
    return rename_into_place(r, temp, name) == SPIFFS_OK;
}

enum { RESUME_FAILED = -1, RESUME_SETTLED = 0, RESUME_VERIFY = 1, RESUME_BLOCKED = 2 };

/* A gap over [offset, size) of a manifest file, or RESUME_BLOCKED when the
 * gap list cannot take it: the entry then stays, unsettled, for a later
 * boot (no check runs meanwhile). */
static int resume_gap(run_t *r, srp_manifest_file_t *f, obj_t *now, uint32_t offset, srp_gap_cause_t cause)
{
    obj_t placeholder;
    memset(&placeholder, 0, sizeof(placeholder));
    memcpy(placeholder.name, f->name, sizeof(placeholder.name));
    placeholder.kind = srp_classify(f->name);
    placeholder.size = f->size;
    obj_t *target = now ? now : &placeholder;
    if (offset < f->size && !record_gap(r, target, offset, f->size - offset, 0, cause)) {
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "records");
        return RESUME_BLOCKED;
    }
    r->report->interrupted_losses++;
    note_file(r, target);
    if (!save_records(r)) return (fail(r, "journal_records", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
    f->flags |= SRP_MANIFEST_REPORTED;
    return RESUME_SETTLED;
}

/* The rows of the manifest's copy under the temporary name (the repair's
 * rebuild, or the original adopted there) go after the rows of the new file
 * a rolled-back image created under the name:
 *  - the new file's own damage is normalized first (its records first): an
 *    interrupted append's bytes past its end would be ANDed into the first
 *    merged row (1.2 item 5), an entry past its end into a page index;
 *  - the copy's unreadable bytes, and what it lacks of the original, are
 *    records at the places its rows take in the new file, before a byte is
 *    written;
 *  - room for the whole append without SPIFFS's GC, or nothing is written;
 *  - where the rows begin is journaled (MERGING): a later boot continues a
 *    merge whose bytes so far read back, or begins again after a file that
 *    changed meanwhile (duplicate rows, never lost ones);
 *  - settled only once every merged byte reads back; the copy stays until
 *    then, and goes afterwards as an expendable.
 * Text files only. */
static int merge_rows(run_t *r, srp_manifest_file_t *f, srp_gap_cause_t cause)
{
    obj_t *now = find_live(r, f->name), *src = find_rebuild(r, f);
    if (!now || !src) return RESUME_VERIFY;
    /* Rounds as normalize_once's: a rewritten span's old page is a stray the
     * next survey shows. */
    for (uint32_t round = 0; now->needs_work && round < SR_NORMALIZE_ROUNDS; ++round) {
        bool gap = false;
        for (uint32_t k = 0; k < now->patches && !gap; ++k)
            for (uint32_t b = 0; b < sizeof(now->patch[k].mask) && !gap; ++b) gap = now->patch[k].mask[b] != 0;
        if (gap) {
            if (!record_salvage_gaps(r, now)) {
                snprintf(r->report->error_stage, sizeof(r->report->error_stage), "records");
                return RESUME_BLOCKED;
            }
            if (!save_records(r)) return (fail(r, "journal_records", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        }
        if (!fill_gaps(r, now)) return (fail(r, "fill_memory", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        bool ok = normalize_object(r, now);
        note_file(r, now);
        if (!ok && r->stopped) return RESUME_FAILED;
        if (!ok) clear_failure(r);
        if (!survey(r)) return RESUME_FAILED;
        now = find_live(r, f->name);
        src = find_rebuild(r, f);
        if (!now || !src) return RESUME_VERIFY;
        if (!ok) break;
    }
    if (now->needs_work) {
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
        return RESUME_BLOCKED;
    }
    if (!fill_gaps(r, src)) return (fail(r, "fill_memory", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
    uint32_t length = src->size, base = 0, done = 0;
    uint8_t last = '\n';
    if (now->size && !object_read(r, now, now->size - 1, &last, 1)) last = '\n';
    bool fresh = true, newline = false;
    if ((f->flags & SRP_MANIFEST_MERGING) && f->restored <= now->size && now->size - f->restored <= length) {
        /* A merge an earlier boot began: its bytes so far read back. */
        int same = same_bytes(r, now, f->restored, src, 0, now->size - f->restored);
        if (same < 0) return (fail(r, "merge_memory", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        if (same) {
            base = f->restored;
            done = now->size - base;
            fresh = false;
        }
    } else if ((f->flags & SRP_MANIFEST_MERGING) && f->restored == now->size + 1 && last != '\n') {
        /* One journaled, its newline not yet written. */
        base = f->restored;
        newline = true;
        fresh = false;
    }
    if (fresh) {
        newline = last != '\n';
        base = now->size + (newline ? 1U : 0U);
        /* Records first, in the new file's offsets. What the copy lacks of
         * the original was recorded already when the rebuild stopped
         * (REPORTED before any merge began). */
        bool tail = length < f->size &&
            (!(f->flags & SRP_MANIFEST_REPORTED) || (f->flags & SRP_MANIFEST_MERGING));
        bool recorded = false;
        uint32_t before = r->report->gaps;
        if (!record_unreadable(r, src, f->name, base, base + length, cause) ||
            (tail && !record_range(r, f->name, base + length, f->size - length, base + length,
                                   SRP_GAP_RESTORE_INTERRUPTED))) {
            snprintf(r->report->error_stage, sizeof(r->report->error_stage), "records");
            return RESUME_BLOCKED;
        }
        recorded = r->report->gaps != before;
        if (recorded && !save_records(r)) return (fail(r, "journal_records", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        if (recorded) f->flags |= SRP_MANIFEST_REPORTED;
        /* Where its rows go, with the records that name those places: a
         * boot that finds no room repeats neither. */
        f->flags |= SRP_MANIFEST_MERGING;
        f->restored = base;
        if (!manifest_save(r)) return (fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
    }
    if (!room_for_append(r, length - done + 1)) {
        if (r->failed) return RESUME_FAILED;
        char message[160], temp[SRP_NAME_BYTES];
        (void)restore_name(f->name, temp);
        snprintf(message, sizeof(message), "%s: no room to append %lu bytes without SPIFFS's GC; they wait as %s",
                 f->name, (unsigned long)(length - done), temp);
        logf_line(r, "WARN", "STORAGE_REPAIR_MERGE_DEFERRED", message);
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
        return RESUME_BLOCKED;
    }
    /* Compaction may have surveyed again. */
    now = find_live(r, f->name);
    src = find_rebuild(r, f);
    if (!now || !src) return RESUME_VERIFY;
    if (!fill_gaps(r, src)) return (fail(r, "fill_memory", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
    bool ok = true;
    if (newline) {
        r->wrote = true;
        spiffs_file fd = SPIFFS_open(r->fs, f->name, SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
        ok = fd >= 0 && SPIFFS_write(r->fs, fd, (void *)"\n", 1) == 1;
        if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
        SPIFFS_clearerr(r->fs);
    }
    if (ok) ok = copy_append(r, f->name, src, done, length);
    /* Settled only once the merged bytes read back. */
    if (!survey(r)) return RESUME_FAILED;
    now = find_live(r, f->name);
    src = find_rebuild(r, f);
    if (now && src && now->size >= base + length && fill_gaps(r, src)) {
        int same = same_bytes(r, now, base, src, 0, length);
        if (same < 0) return (fail(r, "merge_memory", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        if (same) {
            char message[160];
            snprintf(message, sizeof(message), "%s: %lu bytes of the interrupted run appended at %lu, after the new file's rows",
                     f->name, (unsigned long)length, (unsigned long)base);
            logf_line(r, "WARN", "STORAGE_REPAIR_MERGED", message);
            now->actions |= SRR_FILE_REBUILT;
            note_file(r, now);
            r->report->files_rebuilt++;
            return RESUME_SETTLED;
        }
    }
    if (r->failed) return RESUME_FAILED;
    snprintf(r->report->error_stage, sizeof(r->report->error_stage), ok ? "merge" : "full");
    return RESUME_BLOCKED;
}

/* Objects under the temporary name other than `keep` (an earlier run's
 * leftover, a torn copy, or a rebuild the intact original makes redundant),
 * never the original's own object. Surveys again. */
static bool purge_temp(run_t *r, const char *temp, uint16_t keep, uint16_t original)
{
    bool any = false;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (o->header == SR_NO_PAGE || strcmp(o->name, temp) || o->obj == keep || o->obj == original) continue;
        if (!purge_object(r, o)) return fail(r, "purge", SPIFFS_ERR_INTERNAL);
        any = true;
    }
    return !any || survey(r);
}

/* A text file's original or copy that cannot go after a new file's rows
 * (binary): the bytes are a record, the entry settles. */
static int conflicting_name(run_t *r, srp_manifest_file_t *f, obj_t *now)
{
    if (!(f->flags & SRP_MANIFEST_REPORTED)) {
        if (!record_range(r, f->name, 0, f->size, now ? now->size : f->size, SRP_GAP_CHECK_CHANGED) ||
            !save_records(r)) {
            snprintf(r->report->error_stage, sizeof(r->report->error_stage), "records");
            return RESUME_BLOCKED;
        }
        f->flags |= SRP_MANIFEST_REPORTED;
    }
    return RESUME_SETTLED;
}

/* The original under a hidden header waits under the temporary name while a
 * new file has its name: one page, after which SPIFFS's GC moves its pages
 * as any file's (it wipes a hidden one's). rebuild_id names it first. */
static int adopt(run_t *r, srp_manifest_file_t *f, obj_t *hidden)
{
    char temp[SRP_NAME_BYTES];
    if (!restore_name(f->name, temp)) return RESUME_BLOCKED;
    f->flags = (uint8_t)((f->flags | SRP_MANIFEST_RESTORING | SRP_MANIFEST_ADOPTED) & ~SRP_MANIFEST_MERGING);
    f->rebuild_id = hidden->obj;
    f->restored = 0;
    if (!manifest_save(r)) return (fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
    if (unhide_as(r, hidden, temp)) return RESUME_VERIFY;
    if (r->stopped) return RESUME_FAILED;
    r->failed = false;
    snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
    return RESUME_BLOCKED;
}

/* One file of a manifest an interrupted run left, in this order:
 *  - the live file still starts with the manifest's bytes: settled;
 *  - the original whole under a hidden header (the check had begun deleting
 *    it): its header comes back under its name, or under the temporary
 *    name when a new file took its name;
 *  - the manifest's copy under the temporary name (the repair's rebuild,
 *    its pages past the committed size included, or the adopted original):
 *    renamed into place, its missing tail a record first; or, when a new
 *    file has the name, its rows go after the new file's (merge_rows);
 *  - what is left of the original under a hidden header when pages of it
 *    are gone (SPIFFS's GC in an older image's writes): superseded only when
 *    its owner settled the file (the queue cursor's generation moved);
 *    otherwise its unreadable bytes are records and the rest comes back, as
 *    above;
 *  - nothing of it on flash: the repair's own restore was under way
 *    (RESTORING), or its owner replaced the file (the cursor's generation
 *    moved, a new object took the name, or the manifest is a rolled-back
 *    image's), or anything else is reported with its exact range, once.
 * Never settled while the original or its copy is on flash and cannot go
 * back yet: RESUME_BLOCKED keeps the entry (and no check runs). RESUME_VERIFY:
 * the caller surveys and checks the result. */
static int resume_file(run_t *r, srp_manifest_file_t *f, bool stale, srp_gap_cause_t cause)
{
    obj_t *now = find_live(r, f->name);
    if (matches(r, now, f)) return RESUME_SETTLED;
    char temp[SRP_NAME_BYTES];
    bool named = restore_name(f->name, temp);
    obj_t *rebuilt = find_rebuild(r, f);
    bool intact = false;
    obj_t *hidden = hidden_remnant(r, f, &intact);
    if (r->failed) return RESUME_FAILED;
    bool leftover = false;
    for (uint32_t k = 0; named && k < r->obj_count; ++k) {
        const obj_t *o = &r->objs[k];
        leftover |= o->header != SR_NO_PAGE && !strcmp(o->name, temp) && o->obj != f->object_id &&
            (o != rebuilt || intact);
    }
    if (leftover) {
        if (intact && rebuilt) {
            f->flags = (uint8_t)(f->flags & ~(SRP_MANIFEST_RESTORING | SRP_MANIFEST_MERGING | SRP_MANIFEST_ADOPTED));
            f->rebuild_id = 0;
            if (!manifest_save(r)) return (fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        }
        if (!purge_temp(r, temp, intact || !rebuilt ? SR_NO_PAGE : rebuilt->obj, f->object_id)) return RESUME_FAILED;
        now = find_live(r, f->name);
        rebuilt = find_rebuild(r, f);
        hidden = hidden_remnant(r, f, &intact);
        if (r->failed) return RESUME_FAILED;
    }
    if (hidden && intact) {
        if (!now) {
            if (unhide(r, hidden)) {
                hidden->actions |= SRR_FILE_RESTORED;
                note_file(r, hidden);
                return RESUME_VERIFY;
            }
            if (r->stopped) return RESUME_FAILED;
            r->failed = false;
            snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
            return RESUME_BLOCKED;
        }
        return text_fill(hidden) ? adopt(r, f, hidden) : conflicting_name(r, f, now);
    }
    if (rebuilt) {
        if (now) return text_fill(rebuilt) ? merge_rows(r, f, cause) : conflicting_name(r, f, now);
        if (rebuilt->size == f->size && matches(r, rebuilt, f)) {
            if (rename_rebuild(r, rebuilt, temp, f->name)) {
                r->report->files_rebuilt++;
                return RESUME_VERIFY;
            }
        } else {
            /* Stopped part way: its bytes go into place (up to its first
             * unreadable byte), the rest is a record first. */
            if (!(f->flags & SRP_MANIFEST_REPORTED)) {
                uint32_t prefix = next_masked(rebuilt, 0);
                int outcome = resume_gap(r, f, NULL, prefix, SRP_GAP_RESTORE_INTERRUPTED);
                if (outcome != RESUME_SETTLED) return outcome;
                f->restored = prefix;
                if (!manifest_save(r)) return (fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
            }
            if (rename_rebuild(r, rebuilt, temp, f->name)) {
                r->report->files_rebuilt++;
                return RESUME_SETTLED;
            }
        }
        /* Kept under its temporary name for the next boot: its bytes are
         * the only copy. */
        if (r->stopped) return RESUME_FAILED;
        r->failed = false;
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
        return RESUME_BLOCKED;
    }
    srp_kind_t kind = srp_classify(f->name);
    uint32_t generation = 0;
    bool cursor_moved = (f->flags & SRP_MANIFEST_CURSOR) &&
        (!cursor_generation(r, &kind, &generation) || generation != f->cursor_generation);
    if (hidden) {
        if (cursor_moved) {
            supersede(r, f, now, "its queue was retired");
            return RESUME_SETTLED;
        }
        if (now) return text_fill(hidden) ? adopt(r, f, hidden) : conflicting_name(r, f, now);
        if (!(f->flags & SRP_MANIFEST_REPORTED)) {
            if (!record_unreadable(r, hidden, f->name, 0, f->size, cause) ||
                (hidden->size < f->size &&
                 !record_range(r, f->name, hidden->size, f->size - hidden->size, f->size, cause))) {
                snprintf(r->report->error_stage, sizeof(r->report->error_stage), "records");
                return RESUME_BLOCKED;
            }
            if (!save_records(r)) return (fail(r, "journal_records", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
            f->flags |= SRP_MANIFEST_REPORTED;
            if (!manifest_save(r)) return (fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL), RESUME_FAILED);
        }
        if (unhide(r, hidden)) {
            /* Its readable bytes are its file again; normalization fills
             * the recorded ones. */
            hidden->actions |= SRR_FILE_RESTORED;
            note_file(r, hidden);
            return RESUME_SETTLED;
        }
        if (r->stopped) return RESUME_FAILED;
        r->failed = false;
        snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
        return RESUME_BLOCKED;
    }
    bool ours = (f->flags & SRP_MANIFEST_RESTORING) && f->rebuild_id;
    if (ours && !(f->flags & SRP_MANIFEST_REPORTED)) {
        /* The repair's own restore did not finish and nothing of it can be
         * verified: whatever now has the name, those bytes are a record. */
        uint32_t from = now && now->obj == f->rebuild_id && !(f->flags & SRP_MANIFEST_MERGING) ? f->restored : 0;
        return resume_gap(r, f, now, from, SRP_GAP_RESTORE_INTERRUPTED);
    }
    bool replaced = now && now->obj != f->object_id && !(ours && now->obj == f->rebuild_id);
    if (stale || cursor_moved || replaced) {
        supersede(r, f, now, stale ? "another image ran" : cursor_moved ? "its queue was retired" : "a new file");
        return RESUME_SETTLED;
    }
    if (f->flags & SRP_MANIFEST_REPORTED) return RESUME_SETTLED;
    return resume_gap(r, f, now, 0, now ? SRP_GAP_CONTENT_UNVERIFIED : cause);
}

/* A manifest left by an interrupted run (SRP_STATE_MANIFEST): every entry is
 * settled one by one, each saved as it settles, so a reset here repeats only
 * what was not done; the manifest goes once all are. An entry that cannot
 * settle this boot (its record does not fit, or its original cannot come back
 * yet) keeps the manifest, and no check runs. */
static bool resume_manifest(run_t *r, bool *blocked)
{
    *blocked = false;
    if (!manifest_active(r)) return true;
    srp_manifest_t *m = r->manifest;
    if (!srp_manifest_valid(m)) {
        logf_line(r, "ERROR", "STORAGE_REPAIR_MANIFEST_UNREADABLE",
                  "the interrupted run's manifest is unreadable; what it described cannot be verified");
        return manifest_retire(r);
    }
    /* Room first: compaction never touches a hidden original's block. */
    if (!ensure_room(r)) return false;
    /* Stale: another image may have changed the files since. An unconfirmed
     * image's manifest is, unless this is the confirmed image a reset rolled
     * back to: it resumes the manifest before anything else writes. */
    bool pending = (m->flags & SRP_MANIFEST_PENDING_IMAGE) != 0;
    bool rolled_back = pending && m->rollback_tag && !r->cfg->pending_verify && m->rollback_tag == r->cfg->image_tag;
    bool stale = !rolled_back && (pending || m->image_tag != r->cfg->image_tag);
    srp_gap_cause_t cause = r->report->previous_phase == SRP_PHASE_RESTORE ? SRP_GAP_RESTORE_INTERRUPTED
                                                                           : SRP_GAP_CHECK_INTERRUPTED;
    for (uint32_t i = 0; i < m->count; ++i) {
        srp_manifest_file_t *f = &m->file[i];
        if (f->flags & SRP_MANIFEST_SETTLED) continue;
        /* A rebuilt, renamed or unhidden file settles only once a fresh
         * survey reads the manifest's bytes under its name. */
        int outcome = RESUME_VERIFY;
        for (int attempt = 0; outcome == RESUME_VERIFY; ++attempt) {
            if (!survey(r)) return false;
            if (attempt && matches(r, find_live(r, f->name), f)) {
                outcome = RESUME_SETTLED;
                break;
            }
            if (attempt == 3) {
                outcome = resume_gap(r, f, find_live(r, f->name), 0, SRP_GAP_RESTORE_INTERRUPTED);
                break;
            }
            outcome = resume_file(r, f, stale, cause);
        }
        if (outcome == RESUME_FAILED) return false;
        if (outcome == RESUME_BLOCKED) {
            *blocked = true;
            r->failed = false;
            if (!manifest_save(r)) return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
            continue;
        }
        f->flags |= SRP_MANIFEST_SETTLED;
        if (!manifest_save(r)) return fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
    }
    if (*blocked) {
        logf_line(r, "WARN", "STORAGE_REPAIR_RESUME_BLOCKED",
                  "an interrupted run's file could not be settled yet; it stays in the manifest and no check runs");
        return survey(r);
    }
    return manifest_retire(r) && survey(r);
}

static bool exists_name(run_t *r, const char *name)
{
    spiffs_stat st;
    bool found = SPIFFS_stat(r->fs, name, &st) == SPIFFS_OK;
    SPIFFS_clearerr(r->fs);
    return found;
}

/* ---- persistence probe ---------------------------------------------- */
/* Returns SPIFFS_OK, or the first SPIFFS error (SPIFFS_ERR_FULL on a store
 * that is simply full of live data). */
static s32_t probe(run_t *r, const char *name)
{
    uint8_t data[64], back[64];
    for (unsigned i = 0; i < sizeof(data); ++i) data[i] = (uint8_t)(r->state.generation * 31U + i);
    r->wrote = true;
    spiffs_file fd = SPIFFS_open(r->fs, name, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    bool ok = fd >= 0 && SPIFFS_write(r->fs, fd, data, sizeof(data)) == (s32_t)sizeof(data) &&
        SPIFFS_fflush(r->fs, fd) >= 0;
    s32_t error = ok ? SPIFFS_OK : SPIFFS_errno(r->fs);
    if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    if (ok) {
        fd = SPIFFS_open(r->fs, name, SPIFFS_O_RDONLY, 0);
        ok = fd >= 0 && SPIFFS_read(r->fs, fd, back, sizeof(back)) == (s32_t)sizeof(back) &&
            !memcmp(back, data, sizeof(data));
        if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    }
    SPIFFS_clearerr(r->fs);
    if (SPIFFS_remove(r->fs, name) != SPIFFS_OK && exists_name(r, name)) ok = false;
    SPIFFS_clearerr(r->fs);
    if (ok) return SPIFFS_OK;
    return error ? error : SPIFFS_ERR_INTERNAL;
}

/* A header whose size was torn (1.2 item 10) is recovered (rewrite_index
 * logs it) for every file but an expendable, which goes by object id. One
 * whose size no page traces is left as it is and logged here. */
static void report_torn_headers(run_t *r)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (o->hidden || o->header == SR_NO_PAGE || !o->name[0]) continue;
        if (o->plausible || o->kind.cls == SRP_CLASS_EXPENDABLE) continue;
        char message[160];
        snprintf(message, sizeof(message), "%s: header size %lu is torn and no page traces it; file left as it is",
                 o->name, (unsigned long)o->size);
        logf_line(r, "WARN", "STORAGE_REPAIR_SIZE_TORN", message);
        note_file(r, o);
    }
}

/* ---- phases --------------------------------------------------------- */
static bool any_work(run_t *r)
{
    if (r->orphan_count) return true;
    for (uint32_t i = 0; i < r->obj_count; ++i)
        if (live(&r->objs[i]) && r->objs[i].needs_work && !frozen(r, &r->objs[i])) return true;
    return false;
}

/* Gaps first: every unreadable range is in NVS before a byte is written. A
 * file whose ranges cannot all be recorded is left as it is. */
static bool plan_damage(run_t *r)
{
    bool recorded = false;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || !o->needs_work) continue;
        unmask_from_hold(o);
        bool gap = false;
        for (uint32_t k = 0; k < o->patches && !gap; ++k)
            for (uint32_t b = 0; b < sizeof(o->patch[k].mask) && !gap; ++b) gap = o->patch[k].mask[b] != 0;
        /* A copy under the temporary name: the manifest's waits for the
         * resume, which records its unreadable bytes where its rows go; any
         * other is expendable. */
        if (gap && !restore_file(o->name)) {
            uint32_t before = r->report->gaps;
            (void)record_salvage_gaps(r, o);
            if (r->report->gaps != before || o->unrecordable) recorded = true;
            if (o->kind.flags & SRP_COMMAND_FAMILY) r->report->commands_damaged = true;
            if (o->kind.flags & SRP_CATALOG_FAMILY) r->catalog_damaged = true;
            note_file(r, o);
        }
        if (!fill_gaps(r, o)) return fail(r, "fill_memory", SPIFFS_ERR_INTERNAL);
    }
    if (recorded && !save_records(r)) return fail(r, "journal_records", SPIFFS_ERR_INTERNAL);
    /* What the damage obliges later steps to do is journaled before the
     * fill hides it: a run that fails or stops after normalizing must not
     * leave the next one unaware that the catalog is to be dropped or the
     * command inbox merged. */
    uint32_t flags = r->state.flags;
    if (r->catalog_damaged) r->state.flags |= SRP_STATE_CATALOG_CLEAR;
    if (r->report->commands_damaged) r->state.flags |= SRP_STATE_COMMANDS_DAMAGED;
    if (flags != r->state.flags && !save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    return true;
}

/* ---- room ----------------------------------------------------------- */
#define SR_COMPACT_MAX_BLOCKS 48U

/* Pages normalization allocates: new data pages and index page moves. */
static uint32_t pages_for_work(const run_t *r)
{
    uint32_t pages = 2;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        const obj_t *o = &r->objs[i];
        if (!live(o) || !o->needs_work || !o->mapped || frozen((run_t *)r, o)) continue;
        for (uint32_t k = 0; k < o->patches; ++k) pages += o->patch[k].kind != PATCH_REFER;
        for (uint32_t j = 0; j < o->ix_spans; ++j) pages += o->ix_rewrite[j] != 0;
    }
    return pages;
}

/* Free blocks for allocating `pages` outside a GC: SPIFFS refuses to
 * allocate with fewer than two. */
static uint32_t blocks_for(const run_t *r, uint32_t pages)
{
    return 3 + (pages + r->entries - 1) / r->entries;
}

/* Erased pages anywhere, the two blocks SPIFFS keeps for its GC included,
 * counted from the last survey: a lookup entry that says free under a page
 * that is not erased is no room. SPIFFS's own counters drift from the truth
 * when such a page is deleted (it counts it out of the allocated pages it
 * never counted in), so they serve only to follow writes since the survey. */
static int32_t surveyed_free(const run_t *r)
{
    int32_t free = 0;
    for (uint32_t pix = 0; pix < r->total_pages; ++pix)
        if (pix % r->ppb && r->pages[pix].lu == SPIFFS_OBJ_ID_FREE && r->pages[pix].erased) ++free;
    return free;
}
static int32_t counted_free(const run_t *r)
{
    return (int32_t)(r->blocks * r->entries) - (int32_t)r->fs->stats_p_allocated - (int32_t)r->fs->stats_p_deleted;
}

static bool clean_object(const obj_t *o)
{
    return live(o) && o->mapped && o->consistent && !o->needs_work;
}

/* A block's lookup entries in use, and its erased ones. */
static bool block_usage(run_t *r, uint32_t b, uint32_t *used, uint32_t *erased)
{
    if (!hal_read(r, SPIFFS_BLOCK_TO_PADDR(r->fs, b), r->page, r->page_size)) return false;
    *used = *erased = 0;
    for (uint32_t e = 0; e < r->entries; ++e) {
        uint16_t lu = le16(r->page + e * 2);
        if (lu == SPIFFS_OBJ_ID_FREE) ++*erased;
        else if (lu != SPIFFS_OBJ_ID_DELETED) ++*used;
    }
    return true;
}

/* What cleaning a block costs and gains, from the survey. SPIFFS's block
 * cleaning moves every data page, writes the index page of each group of
 * data pages (one object's index span) once, and moves the other index
 * pages; then the block is erased. Erased pages consumed elsewhere: data +
 * groups + other index pages. Net erased pages gained: deleted pages plus
 * the group index pages the block held, less one page per group. */
typedef struct {
    int32_t gain;
    uint32_t consumed, used, groups;
} block_cost_t;

static block_cost_t block_cost(const run_t *r, uint32_t b)
{
    block_cost_t c = {0, 0, 0, 0};
    uint16_t group_obj[16], group_span[16];
    uint32_t groups = 0, data = 0, index = 0, group_index = 0, deleted = 0;
    for (uint32_t e = 0; e < r->entries; ++e) {
        const pg_t *pg = &r->pages[b * r->ppb + 1 + e];
        if (pg->lu == SPIFFS_OBJ_ID_DELETED) ++deleted;
        if (pg->lu == SPIFFS_OBJ_ID_FREE || pg->lu == SPIFFS_OBJ_ID_DELETED || (pg->lu & SR_IX)) continue;
        ++data;
        uint16_t span = (uint16_t)SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, pg->span);
        bool seen = false;
        for (uint32_t g = 0; g < groups && !seen; ++g) seen = group_obj[g] == pg->lu && group_span[g] == span;
        if (!seen && groups < 16) {
            group_obj[groups] = pg->lu;
            group_span[groups++] = span;
        }
    }
    for (uint32_t e = 0; e < r->entries; ++e) {
        const pg_t *pg = &r->pages[b * r->ppb + 1 + e];
        if (pg->lu == SPIFFS_OBJ_ID_FREE || pg->lu == SPIFFS_OBJ_ID_DELETED || !(pg->lu & SR_IX)) continue;
        ++index;
        for (uint32_t g = 0; g < groups; ++g)
            if (group_obj[g] == (uint16_t)(pg->lu & ~SR_IX) && group_span[g] == pg->span) {
                ++group_index;
                break;
            }
    }
    c.gain = (int32_t)(deleted + group_index) - (int32_t)groups;
    c.consumed = data + groups + index - group_index;
    c.used = data + index;
    c.groups = groups;
    return c;
}

/* A deleted page that may be the bytes of a damaged file's imperfect span:
 * the survey reads such pages (and unfinalized ones) as sources. */
static bool byte_source(const run_t *r, const uint16_t *owner, const uint8_t *pinned, const pg_t *pg)
{
    if (pg->obj == SPIFFS_OBJ_ID_FREE || (pg->obj & SR_IX)) return false;
    uint16_t id = (uint16_t)(pg->obj & 0x7FFFU);
    if (!bit(pinned, id)) return false;
    const obj_t *o = owner[id] ? &r->objs[owner[id] - 1] : NULL;
    if (!o || !live(o) || !o->mapped) return true;
    return find_patch((obj_t *)o, pg->span) != NULL;
}

/* A store that has run a while keeps its deleted pages until a write needs
 * the room. SPIFFS's own GC cannot run on a damaged store: in a block it
 * cleans, it points an index entry at whichever page of that span it meets
 * there (a duplicate, an uncommitted copy), deletes data pages whose index
 * page it cannot find, and finds index pages by lookup entry, stale copies
 * included. Its block cleaning runs here only on blocks whose every page is
 * erased, deleted and no byte source of a damaged file, or the referenced
 * page of a consistent file every one of whose lookup entries is a
 * referenced page. Needs a fresh survey. *touched: a cleaning started, so
 * the caller's survey is stale whether it finished or stopped half way. */
static uint32_t compact(run_t *r, uint32_t target, bool *touched)
{
    spiffs *fs = r->fs;
    *touched = false;
    if ((uint32_t)fs->free_blocks >= target) return 0;
    uint16_t *owner = zalloc(r, SR_IDS * sizeof(uint16_t));
    uint8_t *pinned = zalloc(r, SR_IDS / 8U);
    uint8_t *gain = zalloc(r, r->blocks);
    uint32_t cleaned = 0;
    int32_t drift = counted_free(r) - surveyed_free(r);
    if (!owner || !pinned || !gain) goto done;
    for (uint32_t k = 0; k < r->obj_count; ++k) {
        const obj_t *o = &r->objs[k];
        if (o->obj == SR_NO_PAGE) continue;
        uint16_t id = (uint16_t)(o->obj & 0x7FFFU);
        if (owner[id] || !clean_object(o)) set_bit(pinned, id);
        if (!owner[id] || live(o)) owner[id] = (uint16_t)(k + 1);
    }
    for (uint32_t pix = 0; pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        if (pix % r->ppb == 0 || pg->lu == SPIFFS_OBJ_ID_FREE || pg->lu == SPIFFS_OBJ_ID_DELETED) continue;
        uint16_t id = (uint16_t)(pg->lu & 0x7FFFU);
        if (!owner[id] || bit(pinned, id)) continue;
        const obj_t *o = &r->objs[owner[id] - 1];
        bool referenced = pg->obj == pg->lu &&
            ((pg->lu & SR_IX) ? index_valid(pg->flags) && pg->span < o->ix_spans && o->ix[pg->span] == pix
                              : data_valid(pg->flags) && pg->span < o->spans && o->ref[pg->span] == pix);
        if (!referenced) set_bit(pinned, id);
    }
    for (uint32_t b = 0; b < r->blocks; ++b) {
        bool safe = true;
        for (uint32_t e = 0; safe && e < r->entries; ++e) {
            const pg_t *pg = &r->pages[b * r->ppb + 1 + e];
            if (pg->lu == SPIFFS_OBJ_ID_FREE) safe = pg->erased;
            else if (pg->lu == SPIFFS_OBJ_ID_DELETED) safe = !byte_source(r, owner, pinned, pg);
            else {
                uint16_t id = (uint16_t)(pg->lu & 0x7FFFU);
                safe = owner[id] && !bit(pinned, id);
            }
        }
        block_cost_t c = block_cost(r, b);
        gain[b] = safe && c.gain > 0 ? (uint8_t)c.gain : 0;
    }
    while ((uint32_t)fs->free_blocks < target && cleaned < SR_COMPACT_MAX_BLOCKS && going(r)) {
        /* The largest net gain first, among blocks whose moves fit in the
         * erased pages elsewhere. Pages moved into a block's erased tail by
         * an earlier cleaning add their own moves. */
        uint32_t best = r->blocks;
        for (uint32_t b = 0; b < r->blocks; ++b)
            if (gain[b] && (best == r->blocks || gain[b] > gain[best])) best = b;
        if (best == r->blocks) break;
        gain[best] = 0;
        block_cost_t c = block_cost(r, best);
        uint32_t used, erased;
        if (!block_usage(r, best, &used, &erased)) break;
        uint32_t consumed = c.consumed + (used > c.used ? 2 * (used - c.used) : 0);
        /* Room for the moves, and if a cleaning stops half way anyway, for
         * pointing each group's index page at its moved pages. A block of
         * deleted and erased pages alone moves nothing: it is only erased. */
        bool moves = consumed || used;
        if (moves && (int32_t)(consumed + c.groups + 2) > counted_free(r) - drift - (int32_t)erased) continue;
        /* SPIFFS's own block cleaning on a consistent store is upkeep, not a
         * repair: it alone leaves the result CLEAN, writes no probe and lights
         * no repair LED. */
        *touched = true;
        fs->cleaning = 1;
        s32_t res = spiffs_gc_clean(fs, (spiffs_block_ix)best);
        fs->cleaning = 0;
        if (res == SPIFFS_OK) res = spiffs_gc_erase_page_stats(fs, (spiffs_block_ix)best);
        if (res == SPIFFS_OK) res = spiffs_erase_block(fs, (spiffs_block_ix)best);
#if SPIFFS_CACHE
        for (uint32_t i = 0; i < r->ppb; ++i) spiffs_cache_drop_page(fs, (spiffs_page_ix)(best * r->ppb + i));
#endif
        if (res != SPIFFS_OK) {
            /* A cleaning that stopped half way left data pages moved and the
             * index naming the old ones: the caller surveys again, and the
             * check waits until normalization has made that consistent. */
            char message[96];
            snprintf(message, sizeof(message), "block %lu cleaning stopped (%ld); the store is surveyed again",
                     (unsigned long)best, (long)res);
            logf_line(r, "WARN", "STORAGE_REPAIR_COMPACT_STOPPED", message);
            break;
        }
        ++cleaned;
        pace(r);
    }
done:
    release(r, owner);
    release(r, pinned);
    release(r, gain);
    if (cleaned) {
        r->report->blocks_compacted += cleaned;
        char message[96];
        snprintf(message, sizeof(message), "%lu blocks of deleted pages erased; %ld free blocks",
                 (unsigned long)cleaned, (long)fs->free_blocks);
        logf_line(r, "INFO", "STORAGE_REPAIR_COMPACTED", message);
    }
    return cleaned;
}

/* Erased pages to leave after normalizing into the GC reserve: what
 * cleaning the best block (by net gain, once the damaged files are
 * consistent) consumes, so compaction can give the reserve back. A store
 * where no block gains keeps two. */
static uint32_t spare_for_compaction(const run_t *r)
{
    int32_t best_gain = 0;
    uint32_t spare = 2;
    for (uint32_t b = 0; b < r->blocks; ++b) {
        block_cost_t c = block_cost(r, b);
        if (c.gain > best_gain) {
            best_gain = c.gain;
            spare = c.consumed + 2;
        }
    }
    return spare;
}

/* Compaction towards `target` free blocks; whatever it touched is surveyed
 * again, a cleaning that stopped half way included. */
static bool compact_to(run_t *r, uint32_t target)
{
    bool touched = false;
    (void)compact(r, target, &touched);
    return !touched || survey(r);
}

/* The check and SPIFFS's own writes run SPIFFS's GC, which counts on its
 * two free blocks: when a block it cleans needs more pages than are erased,
 * it stops half way with data pages moved and the index on flash still
 * naming the old ones. Compaction gives the blocks back first. */
static bool ensure_room(run_t *r)
{
    if ((uint32_t)r->fs->free_blocks >= 3) return true;
    return survey(r) && compact_to(r, 3);
}

/* Compacts when the work needs more free blocks than the store has; the
 * survey and the gap plan are redone after anything moved. */
static bool make_room(run_t *r)
{
    bool touched = false;
    (void)compact(r, blocks_for(r, pages_for_work(r)), &touched);
    return !touched || (survey(r) && plan_damage(r));
}

/* One normalization: orphans, then every file that needs work; the survey
 * after it can show more (stray pages of spans just rewritten, pages moved
 * away from a torn lookup entry), which the next round takes. */
static bool normalize_once(run_t *r)
{
    for (uint32_t round = 0; round < SR_NORMALIZE_ROUNDS; ++round) {
        for (uint32_t i = 0; i < r->orphan_count; ++i) {
            if (!delete_page(r, r->orphans[i])) return fail(r, "orphan_index", SPIFFS_ERR_INTERNAL);
            r->report->orphan_index_removed++;
            pace(r);
        }
        r->orphan_count = 0;
        for (uint32_t i = 0; i < r->obj_count; ++i) {
            obj_t *o = &r->objs[i];
            if (!live(o) || !o->needs_work) continue;
            if (o->unrecordable || frozen(r, o)) continue;
            if (!normalize_object(r, o)) return false;
            note_file(r, o);
        }
        for (uint32_t i = 0; i < r->obj_count; ++i)
            if (live(&r->objs[i]) && r->objs[i].unrecordable && custody(&r->objs[i])) r->unrecordable_custody = true;
        if (!survey(r)) return false;
        /* Pages normalization moved away from a torn lookup entry. */
        if (any_mislabeled(r) && !(lookup_hygiene(r) && survey(r))) return false;
        bool more = false;
        for (uint32_t i = 0; i < r->obj_count; ++i)
            if (live(&r->objs[i]) && r->objs[i].needs_work && !r->objs[i].unrecordable && !frozen(r, &r->objs[i]))
                more = true;
        if (!more && !r->orphan_count) break;
        if (!plan_damage(r)) return false;
    }
    return true;
}

static bool custody_consistent(const run_t *r)
{
    if (r->unrecordable_custody) return false;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        const obj_t *o = &r->objs[i];
        if (live(o) && custody(o) && (!o->consistent || o->needs_work)) return false;
    }
    return true;
}

/* What a run deferred for room lacked: the pages its work needs, the erased
 * pages there are, and the files that need it (names and sizes). A later run
 * that lacks exactly the same has nothing level 2 could change. */
static uint32_t shortage_signature(run_t *r)
{
    uint32_t values[2] = {pages_for_work(r), (uint32_t)surveyed_free(r)};
    uint32_t crc = srp_crc32(0, values, sizeof(values));
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        const obj_t *o = &r->objs[i];
        if (!live(o) || !o->needs_work) continue;
        crc = srp_crc32(crc, o->name, strlen(o->name));
        crc = srp_crc32(crc, &o->size, sizeof(o->size));
    }
    return crc ? crc : 1;
}

/* Normalization allocates pages; on a store full of live data it can run out
 * of room before the check could free anything. Deleted pages are compacted
 * first. When the damaged files have pages in every block (nothing can be
 * compacted around them) and their work fits in the erased pages SPIFFS
 * keeps for its GC, with what compaction needs to spare, normalization
 * allocates from them as a GC does, and compaction, possible once the files
 * are consistent, gives them back. Otherwise room is made (probe leftovers,
 * cursor stages, the catalog, fully settled queue files), compaction tried
 * again and normalization retried; a store still full defers the check.
 * Nothing is lost: every step left a state the next survey understands. */
static bool normalize_all(run_t *r, bool *deferred)
{
    *deferred = false;
    if (!any_work(r)) {
        *deferred = !custody_consistent(r);
        return true;
    }
    set_phase(r, SRP_PHASE_NORMALIZE, 0, true);
    r->state.phase = SRP_PHASE_NORMALIZE;
    if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    if (!make_room(r)) return false;
    for (int attempt = 0;; ++attempt) {
        uint32_t pages = pages_for_work(r);
        /* Too few free blocks for SPIFFS's own allocation (which refuses
         * below two, after a quick GC that may erase byte sources): the work
         * takes the erased pages SPIFFS keeps for its GC whenever it fits in
         * them, even when compacting the best block afterwards would need
         * more (the store was that crammed before the repair; a custody file
         * SPIFFS cannot read is worse). */
        int32_t erased = surveyed_free(r);
        uint32_t spare = spare_for_compaction(r);
        uint32_t exact = pages - 2; /* pages_for_work's margin left out: what normalization allocates */
        bool reserve = (uint32_t)r->fs->free_blocks < blocks_for(r, pages) && erased >= (int32_t)exact;
        r->fs->cleaning = reserve ? 1 : 0;
        bool ok = normalize_once(r);
        r->fs->cleaning = 0;
        if (ok) {
            if (reserve) {
                char message[140];
                snprintf(message, sizeof(message), "up to %lu pages from the GC reserve (%ld erased%s)",
                         (unsigned long)exact, (long)erased,
                         erased >= (int32_t)(pages + spare) ? "" : "; too few to compact the best block after");
                logf_line(r, "INFO", "STORAGE_REPAIR_RESERVE", message);
                r->report->reserve_pages += exact;
            }
            if ((uint32_t)r->fs->free_blocks < 3 && !compact_to(r, 3)) return false;
            if (!any_work(r) || attempt >= 2) break;
            /* A cleaning that stopped half way: normalize what it left. */
            if (!plan_damage(r)) return false;
            continue;
        }
        if (r->stopped || r->report->error_code != SPIFFS_ERR_FULL) return false;
        clear_failure(r);
        if (attempt == 2) {
            snprintf(r->report->error_stage, sizeof(r->report->error_stage), "full");
            r->report->error_code = SPIFFS_ERR_FULL;
            r->report->room_needed = exact;
            r->report->room_erased = erased > 0 ? (uint32_t)erased : 0;
            *deferred = true;
            r->state.flags |= SRP_STATE_FULL;
            r->state.shortage = shortage_signature(r);
#ifdef SR_TRACE_SHORTAGE
            {
                char trace[140];
                snprintf(trace, sizeof(trace), "deferred: shortage %08lx (%lu needed, %ld erased)", (unsigned long)r->state.shortage,
                         (unsigned long)pages_for_work(r), (long)surveyed_free(r));
                logf_line(r, "DEBUG", "STORAGE_REPAIR_SHORTAGE", trace);
            }
#endif
            char message[140];
            snprintf(message, sizeof(message),
                     "no room to make every file consistent (%lu pages needed, %ld erased); no check this boot",
                     (unsigned long)exact, (long)erased);
            logf_line(r, "WARN", "STORAGE_REPAIR_FULL", message);
            return survey(r);
        }
        /* Level 2 unless the last run deferred for exactly this shortage
         * (the same work, the same erased pages, the same files): then it
         * already ran, and dropping a catalog ADD has sent again buys
         * nothing. Anything else (a queue its owner settled since, files
         * that changed) gets it again. */
        if (attempt == 1 && r->was_full && r->was_shortage == shortage_signature(r)) {
            char message[140];
            snprintf(message, sizeof(message), "no room-making removals: the last run deferred for this shortage (%lu pages needed, %ld erased)",
                     (unsigned long)pages_for_work(r), (long)surveyed_free(r));
            logf_line(r, "INFO", "STORAGE_REPAIR_LEVEL2_SKIPPED", message);
        } else if (attempt == 1) {
#ifdef SR_TRACE_SHORTAGE
            char trace[140];
            snprintf(trace, sizeof(trace), "level 2: was_full %d shortage %08lx now %08lx (%lu needed, %ld erased)", r->was_full,
                     (unsigned long)r->was_shortage, (unsigned long)shortage_signature(r), (unsigned long)pages_for_work(r),
                     (long)surveyed_free(r));
            logf_line(r, "DEBUG", "STORAGE_REPAIR_SHORTAGE", trace);
#endif
            if (r->report->level < 2) r->report->level = 2;
            if (!survey(r) || !remove_expendables_now(r) || !clear_catalog(r, true) || !survey(r) ||
                !reconcile_cursors(r, true))
                return false;
        }
        if (!survey(r)) return false;
        bool touched = false;
        (void)compact(r, blocks_for(r, pages_for_work(r)), &touched);
        if (!survey(r) || !plan_damage(r)) return false;
    }
    *deferred = !custody_consistent(r);
    return true;
}

/* A run that leaves a custody file inconsistent (no room to rewrite it)
 * reports, per file, the spans SPIFFS still cannot read: their bytes are on
 * flash (the repair read them), but the runtime's reads stop there. */
static void report_blocked(run_t *r)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || !custody(o) || !o->mapped || (o->consistent && !o->needs_work) || frozen(r, o)) continue;
        o->blocked_spans = 0;
        o->blocked_offset = UINT32_MAX;
        for (uint32_t k = 0; k < o->patches; ++k) {
            if (o->patch[k].source == SOURCE_TAIL) continue;
            o->blocked_spans++;
            uint32_t at = o->patch[k].span * SRP_SPAN_BYTES;
            if (at < o->blocked_offset) o->blocked_offset = at;
        }
        if (!o->blocked_spans) {
            o->blocked_offset = 0;
            continue;
        }
        note_file(r, o);
        char message[180];
        snprintf(message, sizeof(message),
                 "%s: %lu spans SPIFFS cannot read, the first at offset %lu; their bytes stay on flash until there is room",
                 o->name, (unsigned long)o->blocked_spans, (unsigned long)o->blocked_offset);
        logf_line(r, "WARN", "STORAGE_REPAIR_BLOCKED", message);
    }
}

/* R3, just before the manifest and before every check pass: a fresh survey
 * (compaction or the restore may have moved pages since), normalization of
 * whatever that shows, and every custody file consistent. */
static bool custody_ready(run_t *r, bool *ready)
{
    *ready = false;
    if (!survey(r)) return false;
    if (any_work(r)) {
        bool deferred = false;
        if (!plan_damage(r) || !normalize_all(r, &deferred)) return false;
        if (deferred) return true;
        if (!survey(r)) return false;
    }
    /* No mislabeled page either: the check's handling of one deletes live
     * pages and then whole files (1.2 item 7). */
    if (any_mislabeled(r) && !(lookup_hygiene(r) && survey(r))) return false;
    *ready = custody_consistent(r) && !any_mislabeled(r);
    if (!*ready) logf_line(r, "WARN", "STORAGE_REPAIR_NOT_READY", "a custody file or a lookup entry is not consistent");
    return true;
}

/* Time for one more check pass and the restore after it before the soft
 * deadline: a pass cut by the hard deadline is a reset inside SPIFFS_check
 * (on an unconfirmed image, a rollback). */
static bool time_for_check(run_t *r)
{
    if (!r->ports->remaining_ms) return true;
    uint64_t mib = ((uint64_t)r->blocks * r->block_size + (1U << 20) - 1) >> 20;
    uint32_t per_mib = r->cfg->check_ms_per_mib ? r->cfg->check_ms_per_mib : SR_CHECK_MS_PER_MIB_DEFAULT;
    uint64_t check = r->last_check_ms ? (uint64_t)r->last_check_ms * 5 / 4 : mib * per_mib;
    uint64_t restore = SR_RESTORE_BASE_MS + r->report->bytes_held / SR_RESTORE_BYTES_PER_MS;
    uint64_t left = r->ports->remaining_ms(r->ports->context);
    if (left >= check + restore) return true;
    char message[140];
    snprintf(message, sizeof(message), "%lu s left; a check pass and its restore need about %lu s",
             (unsigned long)(left / 1000), (unsigned long)((check + restore) / 1000));
    logf_line(r, "WARN", "STORAGE_REPAIR_NO_TIME", message);
    return false;
}

static void finish_report(run_t *r)
{
    srr_report_t *rep = r->report;
    rep->check_fix_index = r->fix_index;
    rep->check_fix_lookup = r->fix_lookup;
    rep->check_delete_orphan_index = r->del_orphan;
    rep->check_delete_page = r->del_page;
    rep->check_delete_bad_file = r->del_bad_file;
    rep->check_errors = r->errors;
    rep->check_last_error = r->last_error;
    u32_t total = 0, used = 0;
    if (SPIFFS_info(r->fs, &total, &used) == SPIFFS_OK) {
        rep->total_bytes = total;
        rep->used_after = used;
    }
    SPIFFS_clearerr(r->fs);
    rep->files = 0;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o)) continue;
        rep->files++;
        if (o->actions) note_file(r, o);
    }
}

static bool alloc_run(run_t *r)
{
    r->pages = zalloc(r, r->total_pages * sizeof(pg_t));
    r->block = zalloc(r, r->block_size);
    r->page = zalloc(r, r->page_size);
    r->manifest = zalloc(r, sizeof(srp_manifest_t));
    r->gaps = zalloc(r, sizeof(srp_gaps_t));
    r->resets = zalloc(r, sizeof(srp_resets_t));
    r->span_top = zalloc(r, SR_IDS * sizeof(uint16_t));
    return r->pages && r->block && r->page && r->manifest && r->gaps && r->resets && r->span_top;
}

static void free_run(run_t *r)
{
    for (uint32_t i = 0; r->objs && i < r->obj_count; ++i) {
        release_maps(r, &r->objs[i]);
        drop_hold(r, &r->objs[i]);
    }
    release(r, r->objs);
    release(r, r->hdrs);
    release(r, r->ixpages);
    release(r, r->orphans);
    release(r, r->cand_valid);
    release(r, r->cand_next);
    release(r, r->pages);
    release(r, r->block);
    release(r, r->page);
    release(r, r->manifest);
    release(r, r->gaps);
    release(r, r->resets);
    release(r, r->span_top);
}

/* The checks, with room made between failed passes: probe leftovers, cursor
 * stages and the catalog, then fully settled queue files. Before every pass
 * the store is surveyed again and every custody file must be consistent
 * (R3), and the time left must cover the pass and its restore. After a pass
 * that changed anything, the held files are compared and put back before
 * anything else, whatever the soft deadline says. The manifest is retired
 * once the last pass's restore put every held file back. */
static bool run_checks(run_t *r, bool *catalog_drop, bool *last_failed, bool *cut_short)
{
    *last_failed = false;
    *cut_short = false;
    for (uint32_t pass = 1; pass <= SR_MAX_CHECK_PASSES; ++pass) {
        if (pass > 1) {
            bool ready = false;
            if (!going(r)) break;
            if (!custody_ready(r, &ready)) return false;
            if (!ready || !time_for_check(r)) {
                *cut_short = true;
                logf_line(r, "WARN", "STORAGE_REPAIR_DEFERRED", "no further check pass this boot");
                break;
            }
            r->state.phase = SRP_PHASE_CHECK;
            if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
        }
        bool fixed = false;
        bool passed = check_pass(r, pass, &fixed);
        if (r->ports->after_check && r->ports->after_check(r->ports->context, pass)) fixed = true;
        /* A clean pass changed nothing on flash. */
        if (passed && !fixed) {
            *last_failed = false;
            break;
        }
        r->finishing = true;
        set_phase(r, SRP_PHASE_RESTORE, pass, true);
        r->state.phase = SRP_PHASE_RESTORE;
        bool ok = save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL);
        bool changed = false;
        if (ok) ok = survey(r) && ensure_room(r) && (!any_mislabeled(r) || (lookup_hygiene(r) && survey(r))) &&
            restore_held(r, &changed);
        if (ok && changed) ok = survey(r);
        r->finishing = false;
        if (!ok || r->restore_failed) return false;
        if (!passed) {
            *last_failed = true;
            if (r->report->level == 0) {
                r->report->level = 1;
                remove_expendables(r, true);
                *catalog_drop = true;
                if (!clear_catalog(r, true) || !survey(r)) return false;
            } else if (r->report->level == 1) {
                r->report->level = 2;
                if (!reconcile_cursors(r, true) || !survey(r)) return false;
            } else break;
            if (r->last_error == SPIFFS_ERR_FULL && !ensure_room(r)) return false;
            continue;
        }
        *last_failed = false;
    }
    return manifest_retire(r);
}

void srr_run(spiffs *fs, const srr_config_t *config, const srr_ports_t *ports, srr_report_t *report)
{
    if (!report) return;
    memset(report, 0, sizeof(*report));
    if (!fs || !config || !ports || !ports->alloc || !ports->release) {
        report->result = SRR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "arguments");
        return;
    }
    run_t run;
    memset(&run, 0, sizeof(run));
    run_t *r = &run;
    s_cpu_steps = 0;
    r->fs = fs;
    r->cfg = config;
    r->ports = ports;
    r->report = report;
    uint64_t started = ports->now_ms ? ports->now_ms(ports->context) : 0;
    r->page_size = SPIFFS_CFG_LOG_PAGE_SZ(fs);
    r->block_size = SPIFFS_CFG_LOG_BLOCK_SZ(fs);
    r->ppb = SPIFFS_PAGES_PER_BLOCK(fs);
    r->blocks = fs->block_count;
    r->entries = r->ppb - SPIFFS_OBJ_LOOKUP_PAGES(fs);
    r->total_pages = r->blocks * r->ppb;
    r->hdr_ix = SPIFFS_OBJ_HDR_IX_LEN(fs);
    r->ix_len = SPIFFS_OBJ_IX_LEN(fs);
    u32_t total = 0, used = 0;
    if (SPIFFS_info(fs, &total, &used) == SPIFFS_OK) {
        report->total_bytes = total;
        report->used_before = used;
        report->used_after = used;
    }
    SPIFFS_clearerr(fs);
    if (SPIFFS_OBJ_LOOKUP_PAGES(fs) != 1 || SPIFFS_DATA_PAGE_SIZE(fs) != SRP_SPAN_BYTES || r->page_size > 256 ||
        r->total_pages > 0xFFFFU) {
        report->result = SRR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "geometry");
        return;
    }
    s_run = r;
    if (!alloc_run(r)) {
        report->result = SRR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "memory");
        free_run(r);
        s_run = NULL;
        return;
    }
    /* The journal: where an interrupted run stopped, and the attempts guard. */
    bool have_state = nvs_load(r, SRR_NVS_NAMESPACE, "state", &r->state, sizeof(r->state)) == 1;
    if (!have_state || !srp_state_valid(&r->state)) memset(&r->state, 0, sizeof(r->state));
    bool have_gaps = nvs_load(r, SRR_NVS_NAMESPACE, "gaps", r->gaps, sizeof(*r->gaps)) == 1;
    if (!have_gaps || r->gaps->count > SRP_MAX_GAPS) memset(r->gaps, 0, sizeof(*r->gaps));
    bool have_resets = nvs_load(r, SRR_NVS_NAMESPACE, "resets", r->resets, sizeof(*r->resets)) == 1;
    if (!have_resets || r->resets->count > SRP_MAX_RESETS) memset(r->resets, 0, sizeof(*r->resets));
    report->previous_phase = (srp_phase_t)r->state.phase;
    if (r->state.phase != SRP_PHASE_IDLE) {
        r->state.incomplete++;
        if (config->previous_failed) r->state.failures++;
    }
    /* A manifest stays until every entry settled, whatever phase a reset
     * left: an unreadable one is retired in the resume, with a log line. */
    bool have_manifest = manifest_active(r) &&
        nvs_load(r, SRR_NVS_NAMESPACE, "manifest", r->manifest, sizeof(*r->manifest)) == 1;
    if (manifest_active(r) && !have_manifest) memset(r->manifest, 0xFF, sizeof(*r->manifest));
    if (config->damage_seen) r->state.flags |= SRP_STATE_DAMAGE_SEEN;
    /* FULL describes the last finished run only: this run sets it again
     * when it too defers for room, and every other outcome leaves it clear. */
    r->was_full = (r->state.flags & SRP_STATE_FULL) != 0;
    r->was_shortage = r->state.shortage;
    r->state.flags &= ~SRP_STATE_FULL;
    r->state.shortage = 0;
    srp_policy_input_t input = {
        .incomplete = r->state.incomplete,
        .failures = r->state.failures,
        .disabled = (r->state.flags & SRP_STATE_REPAIR_DISABLED) != 0,
        .same_image = r->state.image_tag == config->image_tag,
        .pending_verify = config->pending_verify,
    };
    srp_policy_t policy = srp_policy(&input);
    report->mode = policy.mode;
    if (policy.clear_disable) {
        r->state.flags &= ~SRP_STATE_REPAIR_DISABLED;
        r->state.failures = 0;
    }
    if (policy.disable) {
        r->state.flags |= SRP_STATE_REPAIR_DISABLED;
        r->state.image_tag = config->image_tag;
    }
    report->incomplete = r->state.incomplete;
    report->failures = r->state.failures;
    char message[200];
    /* The journal's peak must fit beside the page NVS keeps for itself and
     * the runtime's own reserve: a run that fills NVS would fail the
     * runtime's checkpoint commits. */
    uint32_t nvs_needed = srp_nvs_entries_needed(have_state, have_gaps, have_resets, have_manifest);
    bool nvs_short = config->nvs_known && config->nvs_available_entries < nvs_needed;
    if (policy.mode == SRP_MODE_REPORT_ONLY || nvs_short) {
        /* The guard: nothing is read file by file and nothing is written. */
        report->generation = r->state.generation;
        report->result = SRR_RESULT_REPORT_ONLY;
        report->mode = SRP_MODE_REPORT_ONLY;
        report->check_skipped = true;
        if (nvs_short) {
            snprintf(report->error_stage, sizeof(report->error_stage), "nvs_full");
            snprintf(message, sizeof(message),
                     "NVS has %lu available entries; a repair needs %lu, so this boot only reports",
                     (unsigned long)config->nvs_available_entries, (unsigned long)nvs_needed);
            logf_line(r, "ERROR", "STORAGE_REPAIR_NVS_FULL", message);
        } else {
            r->state.phase = SRP_PHASE_IDLE;
            (void)save_state(r);
            snprintf(message, sizeof(message),
                     "storage repairs are off for this image after %lu runs ended by a crash, watchdog or deadline",
                     (unsigned long)r->state.failures);
            logf_line(r, "ERROR", "STORAGE_REPAIR_DISABLED", message);
        }
        free_run(r);
        s_run = NULL;
        return;
    }
    r->state.generation++;
    /* A manifest to settle keeps the phase at RESTORE until it is retired. */
    r->state.phase = manifest_active(r) ? SRP_PHASE_RESTORE : SRP_PHASE_SURVEY;
    report->generation = r->state.generation;
    if (!save_state(r)) {
        /* No journal, no run: the attempts guard could never stop a repair
         * that crashes every boot. Nothing has been written. */
        report->result = SRR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "journal");
        logf_line(r, "ERROR", "STORAGE_REPAIR_JOURNAL_FAILED", "the repair journal could not be saved; nothing repaired");
        free_run(r);
        s_run = NULL;
        return;
    }
    set_phase(r, SRP_PHASE_SURVEY, 0, false);
    snprintf(message, sizeof(message), "run %lu mode %s; previous phase %s; %lu of %lu bytes used",
             (unsigned long)r->state.generation, srp_mode_name(policy.mode),
             srp_phase_name((srp_phase_t)report->previous_phase), (unsigned long)report->used_before,
             (unsigned long)report->total_bytes);
    logf_line(r, "INFO", "STORAGE_REPAIR_STARTED", message);

    bool deferred = false, catalog_drop = false, check_failed = false, checked = false, cut_short = false;
    bool ok = survey(r);
    if (ok) report_torn_headers(r);
    if (ok && any_mislabeled(r)) {
        /* Before the first allocation (the resume's rebuilds included): no
         * used page may look free. The phase stays RESTORE while a manifest
         * is to be settled. */
        set_phase(r, SRP_PHASE_NORMALIZE, 0, true);
        if (!manifest_active(r)) r->state.phase = SRP_PHASE_NORMALIZE;
        ok = (save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL)) && lookup_hygiene(r) && survey(r);
    }
    bool resume_blocked = false;
    if (ok && manifest_active(r)) {
        set_phase(r, SRP_PHASE_RESTORE, 0, true);
        ok = resume_manifest(r, &resume_blocked);
        if (resume_blocked) deferred = true;
    }
    if (ok) ok = plan_damage(r);
    if (ok) hold_custody(r);
    if (ok) {
        bool full = false;
        ok = normalize_all(r, &full);
        deferred |= full;
    }
    if (r->catalog_damaged) catalog_drop = true;
    if (ok) remove_expendables(r, false);
    bool run_check = ok && going(r) && policy.mode == SRP_MODE_FULL && !deferred && !manifest_active(r);
    if (run_check) {
        bool ready = false;
        ok = ensure_room(r) && custody_ready(r, &ready);
        if (ok && !ready) deferred = true;
        else if (ok && !time_for_check(r)) {
            deferred = true;
            snprintf(report->error_stage, sizeof(report->error_stage), "time");
        }
        if (ok && !deferred && going(r)) {
            bool fits = false;
            ok = write_manifest(r, &fits) || fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
            if (ok && !fits) deferred = true;
            else if (ok) {
                checked = true;
                ok = run_checks(r, &catalog_drop, &check_failed, &cut_short);
            }
        }
    }
    if (ok && deferred) {
        logf_line(r, "WARN", "STORAGE_REPAIR_DEFERRED",
                  "a custody file could not be made consistent, or no time is left; no filesystem check this boot");
        report_blocked(r);
    }
    report->check_skipped = !checked;
    if (ok && going(r)) {
        set_phase(r, SRP_PHASE_FINISH, 0, r->wrote);
        r->state.phase = SRP_PHASE_FINISH;
        ok = save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL);
        /* Consistent or not, SPIFFS's own writes follow: room first, as
         * far as compaction can make it safely. */
        if (ok) ok = ensure_room(r);
        if (ok && (catalog_drop || (r->state.flags & SRP_STATE_CATALOG_CLEAR)))
            ok = clear_catalog(r, !r->catalog_damaged) && survey(r);
        if (ok) ok = reconcile_cursors(r, false);
        if (ok) ok = terminate_tails(r);
        if (ok && r->wrote && !check_failed && !deferred && (uint32_t)r->fs->free_blocks < 2) {
            /* SPIFFS's GC runs inside the probe's writes; with fewer than two
             * free blocks it can stop half way through a block (data pages
             * moved, the index naming the originals) and damage a live file. */
            snprintf(report->error_stage, sizeof(report->error_stage), "full");
            report->error_code = SPIFFS_ERR_FULL;
            logf_line(r, "WARN", "STORAGE_REPAIR_FULL",
                      "repaired, but too few free blocks for SPIFFS's own GC; no probe file written");
        } else if (ok && r->wrote && !check_failed && !deferred) {
            s32_t probed = probe(r, "/repair-probe");
            if (probed != SPIFFS_OK && probed != SPIFFS_ERR_FULL) {
                /* A damaged object of that name that SPIFFS will not
                 * recreate (the runtime probe has its alternate too). */
                logf_line(r, "WARN", "STORAGE_REPAIR_PROBE_ALTERNATE", "/repair-probe failed; probing /repair-probe-2");
                probed = probe(r, "/repair-probe-2");
            }
            if (probed == SPIFFS_ERR_FULL) {
                /* Full of live data, not damaged: the runtime's admission
                 * budget handles a full store. */
                snprintf(report->error_stage, sizeof(report->error_stage), "full");
                report->error_code = probed;
                logf_line(r, "WARN", "STORAGE_REPAIR_FULL", "repaired, but the store has no room for a probe file");
            } else if (probed != SPIFFS_OK) ok = fail(r, "probe", probed);
        }
    }
    finish_report(r);
    if (report->commands_damaged) r->state.flags |= SRP_STATE_COMMANDS_DAMAGED;
    if (r->stopped) report->result = SRR_RESULT_STOPPED;
    else if (!ok || r->failed || check_failed) report->result = SRR_RESULT_FAILED;
    else if (deferred || cut_short) report->result = SRR_RESULT_DEFERRED;
    else if (policy.mode == SRP_MODE_NORMALIZE_ONLY) report->result = SRR_RESULT_NORMALIZED;
    else if (report->gaps) report->result = SRR_RESULT_REPAIRED_WITH_GAPS;
    else if (r->wrote) report->result = SRR_RESULT_REPAIRED;
    else report->result = SRR_RESULT_CLEAN;
    if (!r->stopped) {
        /* A finished run, even a failed one: the next boot starts afresh,
         * except that a manifest not yet settled stays (and with it the
         * RESTORE phase, so the next boot resumes it). */
        r->state.phase = manifest_active(r) ? SRP_PHASE_RESTORE : SRP_PHASE_IDLE;
        r->state.incomplete = 0;
        r->state.failures = 0;
        r->state.last_result = report->result;
        if (checked && !check_failed && !cut_short) r->state.flags &= ~SRP_STATE_DAMAGE_SEEN;
        (void)save_state(r);
    }
    report->phase = (srp_phase_t)r->state.phase;
    report->cpu_steps = s_cpu_steps;
    if (ports->now_ms) report->duration_ms = (uint32_t)(ports->now_ms(ports->context) - started);
    snprintf(message, sizeof(message),
             "run %lu %s: %lu files, %lu gaps (%lu bytes), %lu spans rewritten, %lu check passes, %lu cursors reset",
             (unsigned long)report->generation, srr_result_name(report->result), (unsigned long)report->files,
             (unsigned long)report->gaps, (unsigned long)report->unreadable_bytes,
             (unsigned long)report->spans_rewritten, (unsigned long)report->check_passes,
             (unsigned long)report->cursors_reset);
    logf_line(r, report->result == SRR_RESULT_FAILED ? "ERROR"
                 : report->result == SRR_RESULT_CLEAN || report->result == SRR_RESULT_REPAIRED ? "INFO" : "WARN",
              "STORAGE_REPAIR_DONE", message);
    set_phase(r, report->phase, 0, false);
    free_run(r);
    s_run = NULL;
}
