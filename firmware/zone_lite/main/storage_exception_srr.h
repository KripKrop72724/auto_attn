#pragma once
/* ZKT 2.7.7: the boot repair's report as the confirm-first gate reads it
 * (storage_exception.h). Shared by the firmware glue (storage_repair.c) and
 * the SPIFFS host harness, so the harness feeds the gate exactly what the
 * device would. The caller fills what the engine does not know: whether the
 * private mount failed, whether the VFS may mount the store, whether the
 * image is unconfirmed, the attempts guard, ADD's acknowledgements and the
 * redo area's program/erase test. */
#include <stdio.h>
#include <string.h>

#include "storage_exception.h"
#include "storage_repair_core.h"

static inline bool sx_custody_class(uint8_t cls)
{
    return cls == SRP_CLASS_ATTENDANCE || cls == SRP_CLASS_EVIDENCE || cls == SRP_CLASS_OPERATIONAL ||
        cls == SRP_CLASS_DEDUPE || cls == SRP_CLASS_UNKNOWN;
}

static inline void sx_from_srr(const srr_report_t *r, sx_repair_t *out)
{
    memset(out, 0, sizeof(*out));
    if (!r) return;
    out->ran = r->result != SRR_RESULT_NOT_RUN || r->error_stage[0];
    snprintf(out->result, sizeof(out->result), "%s", srr_result_name(r->result));
    snprintf(out->mode, sizeof(out->mode), "%s", srp_mode_name(r->mode));
    snprintf(out->error_stage, sizeof(out->error_stage), "%s", r->error_stage);
    out->error_code = r->error_code;
    out->files_unheld_custody = r->files_unheld_custody;
    out->gaps = r->gaps;
    out->unreadable_bytes = r->unreadable_bytes;
    out->records_dropped = r->records_dropped;
    out->interrupted_losses = r->interrupted_losses;
    out->commands_damaged = r->commands_damaged;
    out->manifest_active = r->manifest_active;
    out->check_deleted_live = r->check_deleted_live;
    out->check_delete_bad_file = r->check_delete_bad_file;
    const srr_reclaim_report_t *x = &r->reclaim;
    snprintf(out->area, sizeof(out->area), "%s", srx_area_name((srx_area_state_t)x->area));
    snprintf(out->trigger, sizeof(out->trigger), "%s", x->wanted ? x->trigger : "");
    snprintf(out->short_reason, sizeof(out->short_reason), "%s", x->short_reason);
    out->rounds = x->rounds;
    out->blocks = x->blocks;
    out->pages_freed = x->pages_freed;
    out->failed = x->failed;
    out->held_mismatch = x->held_mismatch;
    out->records_open = x->records_open;
    out->reported = x->reported;
    out->sources = x->sources;
    out->waiting = x->waiting;
    out->store_blocked = x->store_blocked;
    out->mount_measured = x->mount_measured;
    out->want = x->want;
    out->target = x->target;
    out->custody_gain = x->custody_gain;
    out->custody_blocks = x->custody_blocks;
    out->erased_at_mount = x->erased_at_mount;
    out->file_count = r->file_count < SX_REPAIR_FILES ? r->file_count : SX_REPAIR_FILES;
    for (uint32_t i = 0; i < out->file_count; ++i) {
        const srr_report_file_t *f = &r->file[i];
        sx_repair_file_t *t = &out->file[i];
        snprintf(t->name, sizeof(t->name), "%s", f->name);
        t->custody = sx_custody_class(f->cls);
        t->removed = (f->actions & SRR_FILE_REMOVED) != 0;
        t->unheld = (f->actions & SRR_FILE_UNHELD) != 0;
        t->size = f->size;
        t->blocked_spans = f->blocked_spans;
        t->blocked_offset = f->blocked_offset;
        t->blocked_last = r->blocked_last[i];
        t->blocked_digest = r->blocked_digest[i];
    }
}
