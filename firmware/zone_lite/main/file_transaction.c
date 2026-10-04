#include "file_transaction.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int exists(const char *path)
{
    struct stat st;
    if (stat(path, &st) == 0) return 1;
    return errno == ENOENT ? 0 : -1;
}
static uint32_t extend(uint32_t crc, const unsigned char *data, size_t length)
{
    crc = ~crc;
    while (length--) {
        crc ^= *data++;
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
static bool digest(const char *path, uint32_t length, uint32_t *out)
{
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    unsigned char buffer[512];
    uint32_t crc = 0, remaining = length;
    bool ok = true;
    while (remaining) {
        size_t n = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
        if (fread(buffer, 1, n, f) != n) { ok = false; break; }
        crc = extend(crc, buffer, n); remaining -= (uint32_t)n;
    }
    if (ferror(f)) ok = false;
    if (fclose(f) != 0) ok = false;
    if (ok) *out = crc;
    return ok;
}
static bool matches(const char *path, const ft_checkpoint_t *cp, bool exact)
{
    uint32_t crc;
    struct stat st;
    if (exact && (stat(path, &st) != 0 || (uint64_t)st.st_size != cp->length)) return false;
    return digest(path, cp->length, &crc) && crc == cp->digest;
}
static bool save(ft_port_t port, ft_checkpoint_t *cp, uint32_t phase)
{
    if (cp->generation == UINT32_MAX) return false;
    ft_checkpoint_t next = *cp;
    next.version = 1; next.generation++; next.phase = phase;
    next.crc = dq_crc32(&next, offsetof(ft_checkpoint_t, crc));
    if (!port.commit(port.context, &next)) return false;
    *cp = next;
    return true;
}
static bool load(ft_port_t port, ft_checkpoint_t *cp)
{
    if (!port.load || !port.commit) return false;
    memset(cp, 0, sizeof(*cp));
    int found = port.load(port.context, cp);
    return found == 0 || (found == 1 && cp->version == 1 && cp->generation && cp->phase <= 2 &&
        cp->crc == dq_crc32(cp, offsetof(ft_checkpoint_t, crc)));
}
bool ft_recover(const char *active, const char *stage, const char *backup, ft_port_t port)
{
    ft_checkpoint_t cp;
    if (!active || !stage || !backup || !load(port, &cp)) return false;
    int a = exists(active), b = exists(backup);
    if (a < 0 || b < 0) return false;
    if (!cp.phase) {
        // A legacy backup remains recoverable; never retire it on existence alone.
        if (!a && b) return rename(backup, active) == 0;
        return !b && (a || exists(stage) == 0);
    }
    if (cp.phase == 1) {
        if (!matches(active, &cp, true)) {
            if (!matches(stage, &cp, true)) return false;
            if (a && b) return false; // ambiguous generations remain preserved
            if (a && rename(active, backup) != 0) return false;
            if (rename(stage, active) != 0) return false;
        }
        if (!save(port, &cp, 2)) return false;
    }
    if (!matches(active, &cp, true)) return false;
    b = exists(backup);
    if (b < 0 || (b && remove(backup) != 0)) return false;
    int staged = exists(stage);
    if (staged < 0 || (staged && (!matches(stage, &cp, true) || remove(stage) != 0))) return false;
    return save(port, &cp, 0);
}
bool ft_replace(const char *active, const char *stage, const char *backup, size_t limit, ft_port_t port)
{
    ft_checkpoint_t cp;
    if (!active || !stage || !backup || !load(port, &cp)) return false;
    // Explicit replacement may install a first generation. Recovery alone
    // still cannot promote an uncommitted stage without this owner decision.
    if ((cp.phase || exists(active) != 0 || exists(backup) != 0) &&
        !ft_recover(active, stage, backup, port)) return false;
    struct stat st;
    if (!load(port, &cp) || stat(stage, &st) != 0 || st.st_size < 0 ||
        (uint64_t)st.st_size > limit || (uint64_t)st.st_size > UINT32_MAX) return false;
    FILE *file = fopen(stage, "r+b");
    if (!file) return false;
    bool durable = fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) durable = false;
    if (!durable) return false;
    cp.length = (uint32_t)st.st_size;
    if (!digest(stage, cp.length, &cp.digest) || !save(port, &cp, 1)) return false;
    return ft_recover(active, stage, backup, port);
}

enum {
    FW_LOAD, FW_RECOVER, FW_SCAN, FW_STAGE_CHECK, FW_MOVE_OLD, FW_MOVE_STAGE,
    FW_SAVE_COMMITTED, FW_ACTIVE_CHECK, FW_REMOVE_BACKUP, FW_EXTRA_STAGE,
    FW_REMOVE_STAGE, FW_SAVE_IDLE, FW_PREPARE, FW_SAVE_PREPARED, FW_DONE, FW_FAILED
};
static ft_work_result_t work_result(const ft_work_t *work)
{
    return work->state == FW_DONE ? FT_WORK_DONE : work->state == FW_FAILED ? FT_WORK_FAILED : FT_WORK_PENDING;
}
static ft_work_result_t work_fail(ft_work_t *work, const char *operation, int error)
{
    work->state = FW_FAILED;
    work->operation = operation;
    work->error = error ? error : EIO;
    return FT_WORK_FAILED;
}
static void work_scan(ft_work_t *work, unsigned path, bool compute, unsigned matched, unsigned mismatch)
{
    work->state = FW_SCAN;
    work->path_index = (uint8_t)path;
    work->computing_digest = compute;
    work->scan_position = work->scan_digest = 0;
    work->on_match = (uint8_t)matched;
    work->on_mismatch = (uint8_t)mismatch;
}
bool ft_work_begin(ft_work_t *work, const char *active, const char *stage, const char *backup,
                    size_t limit, ft_port_t port, bool replacement)
{
    if (!work) return false;
    memset(work, 0, sizeof(*work));
    const char *paths[] = {active, stage, backup};
    if (!port.load || !port.commit) { work_fail(work, "transaction_port", EINVAL); return false; }
    for (unsigned i = 0; i < 3; ++i) {
        if (!paths[i] || !paths[i][0] || strlen(paths[i]) >= FT_PATH_BYTES) {
            work_fail(work, "transaction_path", EINVAL); return false;
        }
        for (unsigned j = 0; j < i; ++j)
            if (!strcmp(paths[i], paths[j])) { work_fail(work, "transaction_path_alias", EINVAL); return false; }
        strcpy(work->paths[i], paths[i]);
    }
    work->limit = limit;
    work->port = port;
    work->replacement = replacement;
    work->state = FW_LOAD;
    return true;
}
static ft_work_result_t work_scan_step(ft_work_t *work)
{
    if (work->path_index >= 3 || work->scan_position > work->checkpoint.length)
        return work_fail(work, "transaction_scan_state", EINVAL);
    const char *path = work->paths[work->path_index];
    struct stat st;
    const char *operation = "transaction_scan_stat";
    int error = 0;
    bool ok = stat(path, &st) == 0;
    if (!ok) error = errno;
    else if (st.st_size < 0 || (uint64_t)st.st_size != work->checkpoint.length) { ok = false; error = EINVAL; }
    FILE *file = NULL;
    if (ok) {
        operation = "transaction_scan_open";
        file = fopen(path, "rb");
        if (!file) { ok = false; error = errno; }
    }
    uint32_t position = work->scan_position, crc = work->scan_digest;
    if (file) {
        operation = "transaction_scan_seek";
        if (fseek(file, (long)position, SEEK_SET) != 0) { ok = false; error = errno; }
        uint32_t remaining = work->checkpoint.length - position;
        if (remaining > FT_READ_SLICE_BYTES) remaining = FT_READ_SLICE_BYTES;
        unsigned char buffer[512];
        while (ok && remaining) {
            operation = "transaction_scan_read";
            size_t bytes = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
            errno = 0;
            size_t got = fread(buffer, 1, bytes, file);
            work->bytes_read += got;
            if (got != bytes) { ok = false; error = errno ? errno : EIO; break; }
            crc = extend(crc, buffer, bytes);
            position += (uint32_t)bytes;
            remaining -= (uint32_t)bytes;
        }
        if (ok && ferror(file)) { ok = false; error = errno ? errno : EIO; operation = "transaction_scan_read"; }
        int closed = fclose(file);
        if (ok && closed != 0) { ok = false; error = errno; operation = "transaction_scan_close"; }
    }
    if (!ok) {
        work->state = work->on_mismatch;
        work->operation = operation;
        work->error = error ? error : EIO;
        return work_result(work);
    }
    work->scan_position = position;
    work->scan_digest = crc;
    if (position == work->checkpoint.length) {
        if (work->computing_digest) work->checkpoint.digest = crc;
        bool matched = work->computing_digest || crc == work->checkpoint.digest;
        work->state = matched ? work->on_match : work->on_mismatch;
        if (!matched) { work->operation = "transaction_scan_digest"; work->error = EINVAL; }
    }
    return work_result(work);
}
ft_work_result_t ft_work_step(ft_work_t *work)
{
    if (!work) return FT_WORK_FAILED;
    if (work->state == FW_DONE || work->state == FW_FAILED) return work_result(work);
    if (!work->port.load || !work->port.commit) return work_fail(work, "transaction_not_started", EINVAL);
    work->error = 0;
    work->operation = "transaction_step";
    const char *active = work->paths[0], *stage = work->paths[1], *backup = work->paths[2];
    ft_checkpoint_t *cp = &work->checkpoint;
    int a, b;
    switch (work->state) {
        case FW_LOAD:
            if (!load(work->port, cp)) return work_fail(work, "transaction_checkpoint_load", EIO);
            work->state = FW_RECOVER;
            break;
        case FW_RECOVER:
            a = exists(active);
            if (a < 0) return work_fail(work, "transaction_active_stat", errno);
            b = exists(backup);
            if (b < 0) return work_fail(work, "transaction_backup_stat", errno);
            if (!cp->phase) {
                if (!a && b) {
                    if (rename(backup, active) != 0) return work_fail(work, "transaction_restore_backup", errno);
                } else if (b || (!a && !work->replacement && exists(stage) != 0))
                    return work_fail(work, "transaction_ambiguous_generation", EINVAL);
                work->state = work->replacement ? FW_PREPARE : FW_DONE;
            } else if (cp->phase == 1) work_scan(work, 0, false, FW_SAVE_COMMITTED, FW_STAGE_CHECK);
            else work->state = FW_ACTIVE_CHECK;
            break;
        case FW_SCAN:
            return work_scan_step(work);
        case FW_STAGE_CHECK:
            work_scan(work, 1, false, FW_MOVE_OLD, FW_FAILED);
            break;
        case FW_MOVE_OLD:
            a = exists(active);
            if (a < 0) return work_fail(work, "transaction_active_stat", errno);
            b = exists(backup);
            if (b < 0) return work_fail(work, "transaction_backup_stat", errno);
            if (a && b) return work_fail(work, "transaction_ambiguous_generation", EINVAL);
            if (a && rename(active, backup) != 0) return work_fail(work, "transaction_backup_rename", errno);
            work->state = FW_MOVE_STAGE;
            break;
        case FW_MOVE_STAGE:
            if (rename(stage, active) != 0) return work_fail(work, "transaction_activate_rename", errno);
            work->state = FW_SAVE_COMMITTED;
            break;
        case FW_SAVE_COMMITTED:
            if (!save(work->port, cp, 2)) return work_fail(work, "transaction_commit_generation", EIO);
            work->state = FW_ACTIVE_CHECK;
            break;
        case FW_ACTIVE_CHECK:
            work_scan(work, 0, false, FW_REMOVE_BACKUP, FW_FAILED);
            break;
        case FW_REMOVE_BACKUP:
            b = exists(backup);
            if (b < 0 || (b && remove(backup) != 0)) return work_fail(work, "transaction_remove_backup", errno);
            work->state = FW_EXTRA_STAGE;
            break;
        case FW_EXTRA_STAGE:
            a = exists(stage);
            if (a < 0) return work_fail(work, "transaction_stage_stat", errno);
            if (a) work_scan(work, 1, false, FW_REMOVE_STAGE, FW_FAILED);
            else work->state = FW_SAVE_IDLE;
            break;
        case FW_REMOVE_STAGE:
            if (remove(stage) != 0) return work_fail(work, "transaction_remove_stage", errno);
            work->state = FW_SAVE_IDLE;
            break;
        case FW_SAVE_IDLE:
            if (!save(work->port, cp, 0)) return work_fail(work, "transaction_retire_generation", EIO);
            work->state = work->replacement ? FW_PREPARE : FW_DONE;
            break;
        case FW_PREPARE: {
            struct stat st;
            if (stat(stage, &st) != 0) return work_fail(work, "transaction_candidate_stat", errno);
            if (st.st_size < 0 || (uint64_t)st.st_size > work->limit || (uint64_t)st.st_size > UINT32_MAX)
                return work_fail(work, "transaction_candidate_limit", EFBIG);
            FILE *file = fopen(stage, "r+b");
            if (!file) return work_fail(work, "transaction_candidate_open", errno);
            int error = 0;
            const char *operation = "transaction_candidate_flush";
            if (fflush(file) != 0) error = errno ? errno : EIO;
            if (!error) { operation = "transaction_candidate_sync"; if (fsync(fileno(file)) != 0) error = errno ? errno : EIO; }
            int closed = fclose(file);
            if (!error && closed != 0) { operation = "transaction_candidate_close"; error = errno ? errno : EIO; }
            if (error) return work_fail(work, operation, error);
            cp->length = (uint32_t)st.st_size;
            work_scan(work, 1, true, FW_SAVE_PREPARED, FW_FAILED);
            break;
        }
        case FW_SAVE_PREPARED:
            if (!save(work->port, cp, 1)) return work_fail(work, "transaction_prepare_generation", EIO);
            /* This request's stage is now the recovery intent. Finishing that
             * intent completes the request; never install it a second time. */
            work->replacement = false;
            work->state = FW_RECOVER;
            break;
        default:
            return work_fail(work, "transaction_state", EINVAL);
    }
    return work_result(work);
}
