#include "storage_repair_core.h"

#include <stdio.h>
#include <string.h>

#include "file_transaction.h"
#include "spiffs_nucleus.h"

/* ESP-IDF's SPIFFS layout with 16-bit ids and one lookup page per block, as
 * Zone Lite builds it (page 256, block 4096, data span 251). */
#define SR_IX SPIFFS_OBJ_ID_IX_FLAG
#define SR_NO_PAGE ((uint16_t)0xFFFF)
/* Engine codes, outside SPIFFS's range, for what SPIFFS cannot name. */
#define SR_ERR_ERASED_CONTENT (-10200)   /* a text page holds erased (0xFF) bytes */
#define SR_ERR_LOOKUP_MISMATCH (-10201)  /* the page is right but its lookup entry is not */
#define SR_ROW_WINDOW 8192U              /* longest row a drain reads (DQ_MAX_RECORD_BYTES) */
#define SR_HOLD_CHUNK (64U * 1024U)
#define SR_MAX_OBJECTS 320U
#define SR_MAX_CHECK_PASSES 4U
#define SR_YIELD_EVERY 64U
#define SR_COPY_BYTES 4016U

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

typedef struct {
    char name[SRP_NAME_BYTES];
    srp_kind_t kind;
    uint16_t obj, header;
    uint32_t size, spans, ix_spans;
    bool hidden, plausible, mapped, consistent, needs_work, held, unrecordable, gaps_recorded;
    uint16_t *ix;            /* chosen index page per index span; SR_NO_PAGE when missing */
    uint8_t *ix_rewrite;     /* per index span: rewrite (or create) it */
    uint16_t *ref;           /* referenced page per data span */
    uint8_t *perfect;        /* per data span */
    patch_t *patch;
    uint32_t patches, patch_cap;
    uint16_t *drop;          /* stale headers and duplicate or stale index pages to delete */
    uint32_t drops, drop_cap;
    uint8_t **hold;
    uint32_t hold_chunks, hold_size, crc;
    uint32_t gaps, gap_bytes, rewritten;
    uint8_t actions;
} obj_t;

typedef struct {
    spiffs *fs;
    const sr_config_t *cfg;
    const sr_ports_t *ports;
    sr_report_t *report;
    uint32_t page_size, block_size, ppb, entries, blocks, total_pages, hdr_ix, ix_len;
    pg_t *pages;
    hdr_t *hdrs;
    uint32_t hdr_count, hdr_cap;
    uint16_t *ixpages;
    uint32_t ixpage_count, ixpage_cap;
    uint16_t *orphans;             /* index pages past span 0 of no live file */
    uint32_t orphan_count, orphan_cap;
    obj_t *objs;
    uint32_t obj_count;
    uint8_t *block, *page;
    uint16_t *cand_valid, *cand_deleted, *cand_next; /* per-object candidate lists */
    uint64_t hold_used;
    srp_state_t state;
    srp_manifest_t *manifest, *previous;
    srp_gaps_t *gaps;
    srp_resets_t *resets;
    bool stopped, failed, wrote, catalog_damaged;
    uint32_t ops;
    /* check callback counters */
    uint32_t fix_index, fix_lookup, del_orphan, del_page, del_bad_file, errors;
    int32_t last_error;
    uint16_t deleted_ids[16];
    uint32_t deleted_count;
} run_t;

static run_t *s_run;

const char *sr_result_name(sr_result_t result)
{
    switch (result) {
    case SR_RESULT_NOT_RUN: return "NOT_RUN";
    case SR_RESULT_CLEAN: return "CLEAN";
    case SR_RESULT_REPAIRED: return "REPAIRED";
    case SR_RESULT_REPAIRED_WITH_GAPS: return "REPAIRED_WITH_GAPS";
    case SR_RESULT_DEFERRED: return "DEFERRED";
    case SR_RESULT_NORMALIZED: return "NORMALIZED";
    case SR_RESULT_REPORT_ONLY: return "REPORT_ONLY";
    case SR_RESULT_STOPPED: return "STOPPED";
    case SR_RESULT_FAILED: return "FAILED";
    case SR_RESULT_REFORMATTED: return "REFORMATTED";
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
static bool bit(const uint8_t *mask, uint32_t i) { return (mask[i / 8] >> (i % 8)) & 1U; }
static void set_bit(uint8_t *mask, uint32_t i) { mask[i / 8] |= (uint8_t)(1U << (i % 8)); }

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

static bool going(run_t *r)
{
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
/* A written data page SPIFFS flagged deleted: its bytes stay until the block is erased. */
static bool data_deleted(uint8_t flags)
{
    return !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_DELET)) &&
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
    return nvs_store(r, SR_NVS_NAMESPACE, "state", &r->state, sizeof(r->state));
}
static bool save_records(run_t *r)
{
    return nvs_store(r, SR_NVS_NAMESPACE, "gaps", r->gaps, sizeof(*r->gaps)) &&
        nvs_store(r, SR_NVS_NAMESPACE, "resets", r->resets, sizeof(*r->resets));
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
    o->patches = o->patch_cap = o->drops = o->drop_cap = 0;
    o->mapped = false;
}

static void drop_hold(run_t *r, obj_t *o)
{
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
    static const char *const suffixes[] = {".bak", ".tmp", ".stage", ".backup", ".commit", ".producer", ".spare"};
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

static patch_t *push_patch(run_t *r, obj_t *o)
{
    if (!grow(r, (void **)&o->patch, &o->patch_cap, o->patches, sizeof(patch_t), 4)) return NULL;
    patch_t *p = &o->patch[o->patches++];
    memset(p, 0, sizeof(*p));
    return p;
}

static patch_t *find_patch(obj_t *o, uint32_t span)
{
    for (uint32_t i = 0; i < o->patches; ++i)
        if (o->patch[i].span == span) return &o->patch[i];
    return NULL;
}

static bool acceptable(const obj_t *o, const uint8_t *data, uint32_t length)
{
    return !text_fill(o) || !memchr(data, 0xFF, length);
}

static bool read_payload(run_t *r, uint16_t pix, uint8_t *out, uint32_t length)
{
    return hal_read(r, SPIFFS_PAGE_TO_PADDR(r->fs, pix) + sizeof(spiffs_page_header), out, length);
}

/* Every page of this object by span, in page order: finalized copies and
 * pages flagged deleted. Built once per object that has an imperfect span. */
static bool build_candidates(run_t *r, const obj_t *o)
{
    if (!r->cand_next) {
        r->cand_next = zalloc(r, r->total_pages * sizeof(uint16_t));
        if (!r->cand_next) return false;
    }
    release(r, r->cand_valid);
    release(r, r->cand_deleted);
    r->cand_valid = zalloc(r, o->spans * sizeof(uint16_t));
    r->cand_deleted = zalloc(r, o->spans * sizeof(uint16_t));
    if (!r->cand_valid || !r->cand_deleted) return false;
    memset(r->cand_valid, 0xFF, o->spans * sizeof(uint16_t));
    memset(r->cand_deleted, 0xFF, o->spans * sizeof(uint16_t));
    for (uint32_t pix = r->total_pages; pix-- > 0;) {
        if (pix % r->ppb == 0) continue;
        const pg_t *pg = &r->pages[pix];
        if (pg->obj != o->obj || pg->span >= o->spans) continue;
        uint16_t *head = data_valid(pg->flags) ? &r->cand_valid[pg->span]
                       : data_deleted(pg->flags) ? &r->cand_deleted[pg->span] : NULL;
        if (!head) continue;
        r->cand_next[pix] = *head;
        *head = (uint16_t)pix;
    }
    return true;
}

/* Bytes for an imperfect span: the referenced page when its own header still
 * names this span, another finalized copy, or a page flagged deleted but not
 * erased. Never a page of another object. Otherwise the span is a gap; a
 * text page whose only fault is erased bytes keeps every other byte. */
static bool resolve_span(run_t *r, obj_t *o, uint32_t span, uint16_t ref, int32_t code, patch_t *p)
{
    uint32_t length = span_length(o, span);
    uint8_t candidate[SRP_SPAN_BYTES];
    p->span = span;
    p->length = (uint16_t)length;
    p->code = code;
    p->kind = PATCH_COPY;
    p->source = SOURCE_FILL;
    p->from = SR_NO_PAGE;
    bool last_of_append = (o->kind.flags & SRP_APPEND_TARGET) && span + 1 == o->spans;
    if (page_ok(r, ref) && code != SR_ERR_ERASED_CONTENT) {
        const pg_t *pg = &r->pages[ref];
        if (pg->obj == o->obj && pg->span == span && (pg->flags & SPIFFS_PH_FLAG_INDEX) &&
            !(pg->flags & SPIFFS_PH_FLAG_USED) && (pg->flags & SPIFFS_PH_FLAG_DELET)) {
            /* Ours, but unfinalized or with a wrong lookup entry. */
            if (!read_payload(r, ref, candidate, length)) return false;
            if (acceptable(o, candidate, length)) {
                memcpy(p->data, candidate, length);
                p->source = SOURCE_RELOCATE;
                p->from = ref;
                return true;
            }
        }
    }
    for (uint16_t pix = r->cand_valid[span]; pix != SR_NO_PAGE; pix = r->cand_next[pix]) {
        if (pix == ref) continue;
        if (!read_payload(r, pix, candidate, length)) return false;
        if (!acceptable(o, candidate, length)) continue;
        /* The first acceptable copy, as the check's own search would take. */
        memcpy(p->data, candidate, length);
        p->source = SOURCE_ALTERNATE;
        p->from = pix;
        if (perfect_data(r, o->obj, span, pix) && !last_of_append) {
            p->kind = PATCH_REFER;
            p->refer = pix;
        }
        return true;
    }
    for (int pass = 0; pass < 2; ++pass) {
        for (uint16_t pix = r->cand_deleted[span]; pix != SR_NO_PAGE; pix = r->cand_next[pix]) {
            if ((pass == 0) != (pix == ref)) continue;
            if (!read_payload(r, pix, candidate, length)) return false;
            if (!acceptable(o, candidate, length)) continue;
            memcpy(p->data, candidate, length);
            p->source = SOURCE_DELETED;
            p->from = pix;
            return true;
        }
    }
    if (code == SR_ERR_ERASED_CONTENT && page_ok(r, ref)) {
        if (!read_payload(r, ref, p->data, length)) return false;
        for (uint32_t i = 0; i < length; ++i)
            if (p->data[i] == 0xFF) set_bit(p->mask, i);
        p->source = SOURCE_PARTIAL;
        p->from = ref;
        return true;
    }
    for (uint32_t i = 0; i < length; ++i) set_bit(p->mask, i);
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
    if (r->pages[o->header].lu != (uint16_t)(o->obj | SR_IX)) o->ix_rewrite[0] = 1;
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
            if (!acceptable(o, data, length)) code = SR_ERR_ERASED_CONTENT;
        }
        if (!code) {
            o->perfect[k] = 1;
            continue;
        }
        if (!candidates) {
            if (!build_candidates(r, o)) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
            candidates = true;
        }
        patch_t *p = push_patch(r, o);
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
    /* The last page of an append target must be erased past the end, or the
     * next append is ANDed into whatever an interrupted one left there. */
    if ((o->kind.flags & SRP_APPEND_TARGET) && o->spans && o->size % SRP_SPAN_BYTES && o->perfect[o->spans - 1]) {
        uint32_t k = o->spans - 1, used = o->size % SRP_SPAN_BYTES;
        uint8_t erased[SRP_SPAN_BYTES];
        if (!read_payload(r, o->ref[k], data, SRP_SPAN_BYTES)) return fail(r, "map_read", SPIFFS_ERR_INTERNAL);
        memset(erased, 0xFF, sizeof(erased));
        if (memcmp(data + used, erased, SRP_SPAN_BYTES - used)) {
            patch_t *p = push_patch(r, o);
            if (!p) return fail(r, "map_memory", SPIFFS_ERR_INTERNAL);
            p->span = k;
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

static bool header_better(const hdr_t *a, int32_t score_a, const hdr_t *b, int32_t score_b)
{
    if (score_a != score_b) return score_a > score_b;
    uint32_t size_a = a->size == SPIFFS_UNDEFINED_LEN ? 0 : a->size;
    uint32_t size_b = b->size == SPIFFS_UNDEFINED_LEN ? 0 : b->size;
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
    to->held = from->held;
    to->crc = from->crc;
    to->actions |= from->actions;
    to->gaps = from->gaps;
    to->gap_bytes = from->gap_bytes;
    to->rewritten = from->rewritten;
    to->unrecordable = from->unrecordable;
    to->gaps_recorded = from->gaps_recorded;
    from->hold = NULL;
    from->hold_chunks = 0;
    from->held = false;
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
            o->size = h->size;
            o->hidden = true;
            memcpy(o->name, h->name, sizeof(o->name));
            o->kind = srp_classify(o->name);
            o->plausible = h->named && (h->size == SPIFFS_UNDEFINED_LEN || h->size <= r->fs->cfg.phys_size);
            continue;
        }
        const hdr_t *best = NULL;
        int32_t best_score = 0;
        for (uint32_t k = i; k < r->hdr_count; ++k) {
            const hdr_t *c = &r->hdrs[k];
            if (c->obj != h->obj || c->hidden) continue;
            uint32_t size = c->size == SPIFFS_UNDEFINED_LEN ? 0 : c->size;
            int32_t score = 0;
            if (!score_index(r, h->obj, 0, c->pix, (size + SRP_SPAN_BYTES - 1) / SRP_SPAN_BYTES, &score)) {
                ok = fail(r, "objects_read", SPIFFS_ERR_INTERNAL);
                break;
            }
            if (!best || header_better(c, score, best, best_score)) {
                best = c;
                best_score = score;
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
        o->size = best->size;
        memcpy(o->name, best->name, sizeof(o->name));
        o->kind = srp_classify(o->name);
        o->plausible = best->named && (best->size == SPIFFS_UNDEFINED_LEN || best->size <= r->fs->cfg.phys_size);
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
    /* Data pages of a live file past its end: an append that never
     * committed. The check re-links such a page into the index (FIX_INDEX),
     * and the next append then programs its own page index over that entry
     * in place: the AND of two page indexes, which a later read follows to
     * some other page. (A second copy of a span inside the file is left to
     * the check, which deletes it once the referenced page is perfect.) */
    uint16_t *owner = ok ? zalloc(r, 0x8000U * sizeof(uint16_t)) : NULL;
    if (ok && !owner) ok = fail(r, "objects_memory", SPIFFS_ERR_INTERNAL);
    for (uint32_t k = 0; ok && k < r->obj_count; ++k)
        if (live(&r->objs[k]) && r->objs[k].mapped) owner[r->objs[k].obj & 0x7FFFU] = (uint16_t)(k + 1);
    for (uint32_t pix = 0; ok && pix < r->total_pages; ++pix) {
        const pg_t *pg = &r->pages[pix];
        if (pix % r->ppb == 0 || (pg->obj & SR_IX) || !data_valid(pg->flags)) continue;
        uint16_t index = owner[pg->obj & 0x7FFFU];
        if (!index) continue;
        obj_t *o = &r->objs[index - 1];
        if (pg->span < o->spans) continue;
        ok = push_drop(r, o, (uint16_t)pix);
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

static bool masked(obj_t *o, uint32_t offset)
{
    uint32_t span = offset / SRP_SPAN_BYTES;
    if (span >= o->spans || o->perfect[span]) return false;
    patch_t *p = find_patch(o, span);
    return p && bit(p->mask, offset % SRP_SPAN_BYTES);
}

/* The next unreadable byte at or after offset, or o->size. */
static uint32_t next_masked(obj_t *o, uint32_t offset)
{
    while (offset < o->size) {
        uint32_t span = offset / SRP_SPAN_BYTES;
        if (o->perfect[span]) {
            offset = (span + 1) * SRP_SPAN_BYTES;
            continue;
        }
        if (masked(o, offset)) return offset;
        ++offset;
    }
    return o->size;
}

/* Fills every unreadable run from the readable bytes around it (srp_fill):
 * back to the previous newline and on to the next, within a row's length. */
static bool fill_gaps(run_t *r, obj_t *o)
{
    uint8_t *window = NULL;
    for (uint32_t start = next_masked(o, 0); start < o->size; start = next_masked(o, start)) {
        uint32_t end = start;
        while (end < o->size && masked(o, end)) ++end;
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
        for (uint32_t at = start; at < end; ++at) {
            patch_t *q = find_patch(o, at / SRP_SPAN_BYTES);
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
    sr_report_t *rep = r->report;
    sr_report_file_t *f = NULL;
    for (uint32_t i = 0; i < rep->file_count && !f; ++i)
        if (!strcmp(rep->file[i].name, o->name)) f = &rep->file[i];
    if (!f) {
        if (rep->file_count == SR_REPORT_FILES) return;
        f = &rep->file[rep->file_count++];
        memcpy(f->name, o->name, sizeof(f->name));
        f->cls = (uint8_t)o->kind.cls;
    }
    f->actions |= o->actions;
    f->size = o->size;
    f->gaps = o->gaps;
    f->unreadable_bytes = o->gap_bytes;
    f->spans_rewritten = o->rewritten;
}

static bool record_gap(run_t *r, obj_t *o, uint32_t offset, uint32_t length, int32_t code, srp_gap_cause_t cause)
{
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
    if (!srp_gaps_add(r->gaps, &gap)) {
        r->report->records_dropped++;
        return false;
    }
    r->report->gaps++;
    r->report->unreadable_bytes += length;
    o->gaps++;
    o->gap_bytes += length;
    o->actions |= SR_FILE_GAPS;
    char message[160];
    snprintf(message, sizeof(message), "%s: %lu unreadable bytes at offset %lu (SPIFFS %ld, %s)", o->name,
             (unsigned long)length, (unsigned long)offset, (long)code, srp_gap_cause_name(cause));
    logf_line(r, "WARN", "STORAGE_REPAIR_GAP", message);
    return true;
}

/* Every unreadable run of a file, recorded before anything is written. */
static bool record_salvage_gaps(run_t *r, obj_t *o)
{
    for (uint32_t start = next_masked(o, 0); start < o->size;) {
        uint32_t end = start;
        while (end < o->size && masked(o, end)) ++end;
        patch_t *p = find_patch(o, start / SRP_SPAN_BYTES);
        if (!record_gap(r, o, start, end - start, p ? p->code : 0, SRP_GAP_SALVAGE)) {
            o->unrecordable = true;
            return false;
        }
        start = next_masked(o, end);
    }
    o->gaps_recorded = true;
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
            o->actions |= SR_FILE_UNHELD;
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

static bool any_dirty_free(const run_t *r)
{
    for (uint32_t pix = 0; pix < r->total_pages; ++pix)
        if (pix % r->ppb && r->pages[pix].lu == SPIFFS_OBJ_ID_FREE && !r->pages[pix].erased) return true;
    return false;
}

/* A used page whose lookup entry still says free can be handed out by the
 * allocator and programmed over. A page the survey needs (a header, an index
 * page, a reference or a byte source) gets its lookup entry; any other is
 * deleted. Runs before the first page is allocated. */
static bool lookup_hygiene(run_t *r)
{
    uint8_t *needed = zalloc(r, r->total_pages);
    if (!needed) return fail(r, "hygiene_memory", SPIFFS_ERR_INTERNAL);
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (o->header == SR_NO_PAGE || !o->plausible) continue;
        needed[o->header] = 1;
        for (uint32_t j = 0; o->mapped && j < o->ix_spans; ++j)
            if (o->ix[j] != SR_NO_PAGE) needed[o->ix[j]] = 1;
        for (uint32_t k = 0; o->mapped && k < o->spans; ++k)
            if (page_ok(r, o->ref[k])) needed[o->ref[k]] = 1;
        for (uint32_t k = 0; k < o->patches; ++k)
            if (page_ok(r, o->patch[k].from)) needed[o->patch[k].from] = 1;
    }
    bool ok = true;
    for (uint32_t pix = 0; ok && pix < r->total_pages; ++pix) {
        if (pix % r->ppb == 0) continue;
        pg_t *pg = &r->pages[pix];
        if (pg->lu != SPIFFS_OBJ_ID_FREE || pg->erased) continue;
        bool used = !(pg->flags & SPIFFS_PH_FLAG_USED) && pg->obj != SPIFFS_OBJ_ID_FREE &&
            pg->obj != SPIFFS_OBJ_ID_DELETED;
        ok = needed[pix] && used ? write_lookup(r, pix, pg->obj) : delete_page(r, (uint16_t)pix);
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
    return true;
}

static bool normalize_object(run_t *r, obj_t *o)
{
    if (!o->needs_work) return true;
    for (uint32_t i = 0; i < o->drops; ++i) {
        if (!delete_page(r, o->drop[i])) return fail(r, "drop_page", SPIFFS_ERR_INTERNAL);
        r->report->duplicates_removed++;
        pace(r);
    }
    o->drops = 0;
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
    if (ok) o->actions |= SR_FILE_NORMALIZED;
    return ok;
}

/* ---- the check ------------------------------------------------------ */
void sr_check_callback(spiffs *fs, spiffs_check_type type, spiffs_check_report report, u32_t arg1, u32_t arg2)
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
    set_phase(r, SRP_PHASE_CHECK, pass, true);
    uint64_t started = r->ports->now_ms ? r->ports->now_ms(r->ports->context) : 0;
    s32_t res = SPIFFS_check(r->fs);
    SPIFFS_clearerr(r->fs);
    if (r->ports->now_ms) r->report->check_ms += (uint32_t)(r->ports->now_ms(r->ports->context) - started);
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

/* Probe leftovers always (the runtime probe recreates its own); transient
 * cursor stages only when the store needs room. */
static void remove_expendables(run_t *r, bool for_room)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || o->kind.cls != SRP_CLASS_EXPENDABLE) continue;
        if (!strstr(o->name, "probe") && !for_room) continue;
        if (remove_name(r, o->name)) {
            r->report->expendables_removed++;
            o->actions |= SR_FILE_REMOVED;
            note_file(r, o);
        }
    }
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
static bool clear_catalog(run_t *r)
{
    r->state.flags |= SRP_STATE_CATALOG_CLEAR;
    if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    for (size_t i = 0; i < sizeof(CATALOG_FILES) / sizeof(CATALOG_FILES[0]); ++i) {
        obj_t *o = find_live(r, CATALOG_FILES[i]);
        if (!o) continue;
        if (!remove_name(r, CATALOG_FILES[i])) return fail(r, "catalog_remove", SPIFFS_ERR_INTERNAL);
        o->actions |= SR_FILE_REMOVED;
        note_file(r, o);
    }
    ft_port_t port = {catalog_load, catalog_commit, r};
    if (!ft_checkpoint_idle(port)) return fail(r, "catalog_idle", SPIFFS_ERR_INTERNAL);
    r->state.flags &= ~SRP_STATE_CATALOG_CLEAR;
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
 * remove_settled, a fully settled file is retired as lq_reclaim retires it. */
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
            if (remove_name(r, QUEUES[q].name)) {
                r->report->settled_removed++;
                o->actions |= SR_FILE_REMOVED;
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
    if (!srp_resets_add(r->resets, &reset)) r->report->records_dropped++;
    if (!save_records(r) || !nvs_store(r, QUEUES[q].ns, QUEUES[q].key, &next, sizeof(next)))
        return fail(r, "cursor_commit", SPIFFS_ERR_INTERNAL);
    r->report->cursors_reset++;
    if (o) {
        o->actions |= SR_FILE_CURSOR_RESET;
        note_file(r, o);
    }
    char message[180];
    snprintf(message, sizeof(message), "%s: cursor %lu (generation %lu) cleared, %s; every retained row replays",
             QUEUES[q].name, (unsigned long)cp.offset, (unsigned long)cp.generation, srp_cursor_verdict_name(verdict));
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
static bool terminate_tails(run_t *r)
{
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || !o->size || !o->consistent) continue;
        bool command = (o->kind.flags & SRP_COMMAND_FAMILY) != 0;
        if (!(o->kind.flags & SRP_APPEND_TARGET) && !command) continue;
        uint8_t last;
        if (!object_read(r, o, o->size - 1, &last, 1) || last == '\n') continue;
        if (command) {
            r->report->commands_damaged = true;
            continue;
        }
        if (settled_offset(r, o) >= o->size) continue; /* settled: the drain retires it */
        r->wrote = true;
        spiffs_file fd = SPIFFS_open(r->fs, o->name, SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
        bool ok = fd >= 0 && SPIFFS_write(r->fs, fd, (void *)"\n", 1) == 1 && SPIFFS_fflush(r->fs, fd) >= 0;
        if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
        SPIFFS_clearerr(r->fs);
        if (!ok) return fail(r, "tail_append", SPIFFS_ERR_INTERNAL);
        o->actions |= SR_FILE_TAIL;
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
static bool rebuild_from_hold(run_t *r, obj_t *o)
{
    if (!o->hold) return false;
    r->wrote = true;
    spiffs_file fd = SPIFFS_open(r->fs, o->name, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    bool ok = fd >= 0;
    uint8_t *chunk = ok ? zalloc(r, SR_COPY_BYTES) : NULL;
    if (!chunk) ok = false;
    for (uint32_t offset = 0; ok && offset < o->hold_size;) {
        uint32_t n = o->hold_size - offset < SR_COPY_BYTES ? o->hold_size - offset : SR_COPY_BYTES;
        ok = held_read(o, offset, chunk, n) && SPIFFS_write(r->fs, fd, chunk, (s32_t)n) == (s32_t)n;
        offset += n;
        pace(r);
    }
    if (ok) ok = SPIFFS_fflush(r->fs, fd) >= 0;
    if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    SPIFFS_clearerr(r->fs);
    release(r, chunk);
    if (!ok) return fail(r, "rebuild", SPIFFS_ERR_INTERNAL);
    o->actions |= SR_FILE_REBUILT;
    r->report->files_rebuilt++;
    note_file(r, o);
    return true;
}

/* Puts back what the check changed in a held file: differing spans in place,
 * or the whole file when the check removed or resized it. */
static bool restore_held(run_t *r, bool *changed)
{
    *changed = false;
    uint8_t now_bytes[SRP_SPAN_BYTES], held_bytes[SRP_SPAN_BYTES];
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!o->held || !o->hold) continue;
        if (!live(o) || o->size != o->hold_size) {
            *changed = true;
            if (live(o) && !remove_name(r, o->name)) return fail(r, "restore_remove", SPIFFS_ERR_INTERNAL);
            if (!rebuild_from_hold(r, o)) return false;
            continue;
        }
        release(r, o->patch);
        o->patch = NULL;
        o->patches = o->patch_cap = 0;
        o->drops = 0;
        memset(o->ix_rewrite, 0, o->ix_spans);
        bool differs = false;
        for (uint32_t k = 0; k < o->spans; ++k) {
            uint32_t length = span_length(o, k);
            if (o->perfect[k] && object_read(r, o, k * SRP_SPAN_BYTES, now_bytes, length) &&
                held_read(o, k * SRP_SPAN_BYTES, held_bytes, length) && !memcmp(now_bytes, held_bytes, length))
                continue;
            patch_t *p = push_patch(r, o);
            if (!p || !held_read(o, k * SRP_SPAN_BYTES, p->data, length)) return fail(r, "restore", SPIFFS_ERR_INTERNAL);
            p->span = k;
            p->length = (uint16_t)length;
            p->kind = PATCH_COPY;
            p->source = SOURCE_HELD;
            p->filled = 1;
            o->perfect[k] = 0;
            o->ix_rewrite[SPIFFS_OBJ_IX_ENTRY_SPAN_IX(r->fs, k)] = 1;
            differs = true;
        }
        if (!differs) continue;
        *changed = true;
        o->needs_work = true;
        if (!normalize_object(r, o)) return false;
        o->actions |= SR_FILE_RESTORED;
        r->report->files_restored++;
        note_file(r, o);
    }
    return true;
}

/* ---- manifest (resume) ---------------------------------------------- */
static bool write_manifest(run_t *r)
{
    srp_manifest_t *m = r->manifest;
    memset(m, 0, sizeof(*m));
    m->generation = r->state.generation;
    for (uint32_t i = 0; i < r->obj_count && m->count < SRP_MANIFEST_FILES; ++i) {
        obj_t *o = &r->objs[i];
        if (!o->held || !live(o) || o->kind.cls == SRP_CLASS_DEDUPE || o->kind.cls == SRP_CLASS_UNKNOWN) continue;
        srp_manifest_file_t *f = &m->file[m->count++];
        memcpy(f->name, o->name, sizeof(f->name));
        f->cls = (uint8_t)o->kind.cls;
        f->object_id = o->obj;
        f->size = o->size;
        f->crc = o->crc;
    }
    srp_manifest_seal(m);
    if (!nvs_store(r, SR_NVS_NAMESPACE, "manifest", m, sizeof(*m))) return false;
    r->state.flags |= SRP_STATE_MANIFEST;
    r->report->manifest_written = true;
    return save_state(r);
}

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

/* After a reset during a check or a restore: a file the previous run held
 * and the store no longer has intact. One the check had begun deleting is
 * rebuilt from its own pages, which stay until the next check; anything else
 * is reported with its exact range. */
static bool resume_manifest(run_t *r)
{
    srp_manifest_t *m = r->previous;
    if (!srp_manifest_valid(m) || !m->count) return true;
    srp_gap_cause_t cause = r->report->previous_phase == SRP_PHASE_RESTORE ? SRP_GAP_RESTORE_INTERRUPTED
                                                                           : SRP_GAP_CHECK_INTERRUPTED;
    bool rebuilt = false;
    for (uint32_t i = 0; i < m->count; ++i) {
        const srp_manifest_file_t *f = &m->file[i];
        obj_t *now = find_live(r, f->name);
        uint32_t crc;
        if (now && now->size >= f->size && object_crc(r, now, f->size, &crc) && crc == f->crc) continue;
        obj_t *hidden = NULL;
        for (uint32_t k = 0; k < r->obj_count && !hidden; ++k) {
            obj_t *o = &r->objs[k];
            if (o->hidden && o->header != SR_NO_PAGE && o->plausible && o->obj == f->object_id &&
                !strcmp(o->name, f->name))
                hidden = o;
        }
        if (hidden && map_object(r, hidden) && fill_gaps(r, hidden) && hidden->size == f->size &&
            object_crc(r, hidden, f->size, &crc) && crc == f->crc && take_hold(r, hidden)) {
            if (now && !remove_name(r, f->name)) return fail(r, "resume_remove", SPIFFS_ERR_INTERNAL);
            if (!rebuild_from_hold(r, hidden)) return false;
            drop_hold(r, hidden);
            rebuilt = true;
            continue;
        }
        if (r->failed) return false;
        obj_t placeholder;
        memset(&placeholder, 0, sizeof(placeholder));
        memcpy(placeholder.name, f->name, sizeof(placeholder.name));
        placeholder.kind = srp_classify(f->name);
        placeholder.size = f->size;
        obj_t *target = now ? now : &placeholder;
        (void)record_gap(r, target, 0, f->size, 0, now ? SRP_GAP_CONTENT_UNVERIFIED : cause);
        r->report->interrupted_losses++;
        note_file(r, target);
    }
    if (!save_records(r)) return fail(r, "journal_records", SPIFFS_ERR_INTERNAL);
    return !rebuilt || survey(r);
}

/* ---- persistence probe ---------------------------------------------- */
static bool probe(run_t *r)
{
    static const char name[] = "/repair-probe";
    uint8_t data[64], back[64];
    for (unsigned i = 0; i < sizeof(data); ++i) data[i] = (uint8_t)(r->state.generation * 31U + i);
    r->wrote = true;
    spiffs_file fd = SPIFFS_open(r->fs, name, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    bool ok = fd >= 0 && SPIFFS_write(r->fs, fd, data, sizeof(data)) == (s32_t)sizeof(data) &&
        SPIFFS_fflush(r->fs, fd) >= 0;
    if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    if (ok) {
        fd = SPIFFS_open(r->fs, name, SPIFFS_O_RDONLY, 0);
        ok = fd >= 0 && SPIFFS_read(r->fs, fd, back, sizeof(back)) == (s32_t)sizeof(back) &&
            !memcmp(back, data, sizeof(data));
        if (fd >= 0 && SPIFFS_close(r->fs, fd) < 0) ok = false;
    }
    if (SPIFFS_remove(r->fs, name) != SPIFFS_OK) ok = false;
    SPIFFS_clearerr(r->fs);
    return ok;
}

/* ---- phases --------------------------------------------------------- */
static bool any_work(run_t *r)
{
    if (r->orphan_count) return true;
    for (uint32_t i = 0; i < r->obj_count; ++i)
        if (live(&r->objs[i]) && r->objs[i].needs_work) return true;
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
        bool gap = false;
        for (uint32_t k = 0; k < o->patches && !gap; ++k)
            for (uint32_t b = 0; b < sizeof(o->patch[k].mask) && !gap; ++b) gap = o->patch[k].mask[b] != 0;
        if (gap && !o->gaps_recorded) {
            (void)record_salvage_gaps(r, o);
            recorded = true;
            if (o->kind.flags & SRP_COMMAND_FAMILY) r->report->commands_damaged = true;
            if (o->kind.flags & SRP_CATALOG_FAMILY) r->catalog_damaged = true;
            note_file(r, o);
        }
        if (!fill_gaps(r, o)) return fail(r, "fill_memory", SPIFFS_ERR_INTERNAL);
    }
    if (recorded && !save_records(r)) return fail(r, "journal_records", SPIFFS_ERR_INTERNAL);
    return true;
}

static bool normalize_all(run_t *r, bool *deferred)
{
    *deferred = false;
    if (!any_work(r)) return true;
    set_phase(r, SRP_PHASE_NORMALIZE, 0, true);
    r->state.phase = SRP_PHASE_NORMALIZE;
    if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    for (uint32_t i = 0; i < r->orphan_count; ++i) {
        if (!delete_page(r, r->orphans[i])) return fail(r, "orphan_index", SPIFFS_ERR_INTERNAL);
        r->report->orphan_index_removed++;
        pace(r);
    }
    r->orphan_count = 0;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (!live(o) || !o->needs_work) continue;
        if (o->unrecordable) {
            *deferred |= custody(o);
            continue;
        }
        if (!normalize_object(r, o)) return false;
        note_file(r, o);
    }
    if (!survey(r)) return false;
    for (uint32_t i = 0; i < r->obj_count; ++i) {
        obj_t *o = &r->objs[i];
        if (live(o) && !o->consistent && custody(o)) *deferred = true;
    }
    return true;
}

static void finish_report(run_t *r)
{
    sr_report_t *rep = r->report;
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
    r->previous = zalloc(r, sizeof(srp_manifest_t));
    r->gaps = zalloc(r, sizeof(srp_gaps_t));
    r->resets = zalloc(r, sizeof(srp_resets_t));
    return r->pages && r->block && r->page && r->manifest && r->previous && r->gaps && r->resets;
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
    release(r, r->cand_deleted);
    release(r, r->cand_next);
    release(r, r->pages);
    release(r, r->block);
    release(r, r->page);
    release(r, r->manifest);
    release(r, r->previous);
    release(r, r->gaps);
    release(r, r->resets);
}

/* The checks, with room made between failed passes: probe leftovers, cursor
 * stages and the catalog, then fully settled queue files. Empty blocks are
 * reclaimed only after a pass that ran out of room, never between a check
 * and the restore of what it changed. */
static bool run_checks(run_t *r, bool *catalog_drop, bool *last_failed)
{
    *last_failed = false;
    uint32_t pass = 0;
    while (pass < SR_MAX_CHECK_PASSES) {
        bool fixed = false;
        bool passed = check_pass(r, ++pass, &fixed);
        if (!going(r)) return false;
        /* A clean pass changed nothing on flash. */
        if (passed && !fixed) {
            *last_failed = false;
            return true;
        }
        if (!survey(r)) return false;
        set_phase(r, SRP_PHASE_RESTORE, pass, true);
        r->state.phase = SRP_PHASE_RESTORE;
        if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
        bool changed = false;
        if (!restore_held(r, &changed)) return false;
        if (changed && !survey(r)) return false;
        if (!passed) {
            *last_failed = true;
            if (r->report->level == 0) {
                r->report->level = 1;
                remove_expendables(r, true);
                *catalog_drop = true;
                if (!clear_catalog(r) || !survey(r)) return false;
            } else if (r->report->level == 1) {
                r->report->level = 2;
                if (!reconcile_cursors(r, true) || !survey(r)) return false;
            } else return true;
            if (r->last_error == SPIFFS_ERR_FULL) (void)spiffs_gc_quick(r->fs, 0);
            r->state.phase = SRP_PHASE_CHECK;
            if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
            continue;
        }
        *last_failed = false;
        r->state.phase = SRP_PHASE_CHECK;
        if (!save_state(r)) return fail(r, "journal", SPIFFS_ERR_INTERNAL);
    }
    return true;
}

void sr_run(spiffs *fs, const sr_config_t *config, const sr_ports_t *ports, sr_report_t *report)
{
    if (!report) return;
    memset(report, 0, sizeof(*report));
    if (!fs || !config || !ports || !ports->alloc || !ports->release) {
        report->result = SR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "arguments");
        return;
    }
    run_t run;
    memset(&run, 0, sizeof(run));
    run_t *r = &run;
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
        report->result = SR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "geometry");
        return;
    }
    s_run = r;
    if (!alloc_run(r)) {
        report->result = SR_RESULT_FAILED;
        snprintf(report->error_stage, sizeof(report->error_stage), "memory");
        free_run(r);
        s_run = NULL;
        return;
    }
    /* The journal: where an interrupted run stopped, and the attempts guard. */
    if (nvs_load(r, SR_NVS_NAMESPACE, "state", &r->state, sizeof(r->state)) != 1 || !srp_state_valid(&r->state))
        memset(&r->state, 0, sizeof(r->state));
    if (nvs_load(r, SR_NVS_NAMESPACE, "gaps", r->gaps, sizeof(*r->gaps)) != 1 || r->gaps->count > SRP_MAX_GAPS)
        memset(r->gaps, 0, sizeof(*r->gaps));
    if (nvs_load(r, SR_NVS_NAMESPACE, "resets", r->resets, sizeof(*r->resets)) != 1 ||
        r->resets->count > SRP_MAX_RESETS)
        memset(r->resets, 0, sizeof(*r->resets));
    report->previous_phase = (srp_phase_t)r->state.phase;
    if (r->state.phase != SRP_PHASE_IDLE) {
        r->state.incomplete++;
        if (config->previous_failed) r->state.failures++;
    }
    if (!(r->state.flags & SRP_STATE_MANIFEST) ||
        nvs_load(r, SR_NVS_NAMESPACE, "manifest", r->previous, sizeof(*r->previous)) != 1 ||
        !srp_manifest_valid(r->previous))
        memset(r->previous, 0, sizeof(*r->previous));
    if (config->damage_seen) r->state.flags |= SRP_STATE_DAMAGE_SEEN;
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
    if (policy.mode == SRP_MODE_REPORT_ONLY) {
        /* The guard: nothing is read file by file and nothing is written. */
        r->state.phase = SRP_PHASE_IDLE;
        (void)save_state(r);
        report->generation = r->state.generation;
        report->result = SR_RESULT_REPORT_ONLY;
        report->check_skipped = true;
        snprintf(message, sizeof(message),
                 "storage repairs are off for this image after %lu runs ended by a crash, watchdog or deadline",
                 (unsigned long)r->state.failures);
        logf_line(r, "ERROR", "STORAGE_REPAIR_DISABLED", message);
        free_run(r);
        s_run = NULL;
        return;
    }
    r->state.generation++;
    r->state.phase = SRP_PHASE_SURVEY;
    report->generation = r->state.generation;
    if (!save_state(r)) fail(r, "journal", SPIFFS_ERR_INTERNAL);
    set_phase(r, SRP_PHASE_SURVEY, 0, false);
    snprintf(message, sizeof(message), "run %lu mode %s; previous phase %s; %lu of %lu bytes used",
             (unsigned long)r->state.generation, srp_mode_name(policy.mode),
             srp_phase_name((srp_phase_t)report->previous_phase), (unsigned long)report->used_before,
             (unsigned long)report->total_bytes);
    logf_line(r, "INFO", "STORAGE_REPAIR_STARTED", message);

    bool deferred = false, catalog_drop = false, check_failed = false;
    bool ok = survey(r);
    if (ok && (r->state.flags & SRP_STATE_CATALOG_CLEAR)) ok = clear_catalog(r) && survey(r);
    if (ok && report->previous_phase >= SRP_PHASE_CHECK && report->previous_phase <= SRP_PHASE_RESTORE)
        ok = resume_manifest(r);
    if (ok && any_dirty_free(r)) {
        /* Before the first allocation: no used page may look free. */
        set_phase(r, SRP_PHASE_NORMALIZE, 0, true);
        r->state.phase = SRP_PHASE_NORMALIZE;
        ok = (save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL)) && lookup_hygiene(r) && survey(r);
    }
    if (ok) ok = plan_damage(r);
    if (ok) hold_custody(r);
    if (ok) ok = normalize_all(r, &deferred);
    if (r->catalog_damaged) catalog_drop = true;
    if (ok && deferred)
        logf_line(r, "WARN", "STORAGE_REPAIR_DEFERRED",
                  "a custody file could not be made consistent; no filesystem check this boot");
    if (ok) remove_expendables(r, false);
    bool run_check = ok && !r->stopped && policy.mode == SRP_MODE_FULL && !deferred;
    report->check_skipped = !run_check;
    if (run_check) {
        r->state.phase = SRP_PHASE_CHECK;
        ok = write_manifest(r) || fail(r, "journal_manifest", SPIFFS_ERR_INTERNAL);
        if (ok) ok = run_checks(r, &catalog_drop, &check_failed);
    }
    if (ok && !r->stopped) {
        set_phase(r, SRP_PHASE_FINISH, 0, true);
        r->state.phase = SRP_PHASE_FINISH;
        ok = save_state(r) || fail(r, "journal", SPIFFS_ERR_INTERNAL);
        if (ok && (catalog_drop || (r->state.flags & SRP_STATE_CATALOG_CLEAR)))
            ok = clear_catalog(r) && survey(r);
        if (ok) ok = reconcile_cursors(r, false);
        if (ok) ok = terminate_tails(r);
        if (ok && r->wrote && !check_failed && !deferred && !probe(r)) ok = fail(r, "probe", SPIFFS_ERR_INTERNAL);
    }
    finish_report(r);
    if (report->commands_damaged) r->state.flags |= SRP_STATE_COMMANDS_DAMAGED;
    if (r->stopped) report->result = SR_RESULT_STOPPED;
    else if (!ok || r->failed || check_failed) report->result = SR_RESULT_FAILED;
    else if (deferred) report->result = SR_RESULT_DEFERRED;
    else if (policy.mode == SRP_MODE_NORMALIZE_ONLY) report->result = SR_RESULT_NORMALIZED;
    else if (report->gaps) report->result = SR_RESULT_REPAIRED_WITH_GAPS;
    else if (r->wrote) report->result = SR_RESULT_REPAIRED;
    else report->result = SR_RESULT_CLEAN;
    if (!r->stopped) {
        /* A finished run, even a failed one: the next boot starts afresh. */
        r->state.phase = SRP_PHASE_IDLE;
        r->state.incomplete = 0;
        r->state.failures = 0;
        r->state.last_result = report->result;
        if (r->state.flags & SRP_STATE_MANIFEST) {
            if (ports->nvs_erase) (void)ports->nvs_erase(ports->context, SR_NVS_NAMESPACE, "manifest");
            r->state.flags &= ~SRP_STATE_MANIFEST;
        }
        if (!report->check_skipped && !check_failed) r->state.flags &= ~SRP_STATE_DAMAGE_SEEN;
        (void)save_state(r);
    }
    report->phase = (srp_phase_t)r->state.phase;
    if (ports->now_ms) report->duration_ms = (uint32_t)(ports->now_ms(ports->context) - started);
    snprintf(message, sizeof(message),
             "run %lu %s: %lu files, %lu gaps (%lu bytes), %lu spans rewritten, %lu check passes, %lu cursors reset",
             (unsigned long)report->generation, sr_result_name(report->result), (unsigned long)report->files,
             (unsigned long)report->gaps, (unsigned long)report->unreadable_bytes,
             (unsigned long)report->spans_rewritten, (unsigned long)report->check_passes,
             (unsigned long)report->cursors_reset);
    logf_line(r, report->result == SR_RESULT_FAILED ? "ERROR"
                 : report->result == SR_RESULT_CLEAN || report->result == SR_RESULT_REPAIRED ? "INFO" : "WARN",
              "STORAGE_REPAIR_DONE", message);
    set_phase(r, report->phase, 0, false);
    free_run(r);
    s_run = NULL;
}
