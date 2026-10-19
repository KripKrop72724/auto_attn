#include "command_inbox_merge.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* The last byte of an id slot marks a command the merge file already holds. */
#define CIM_IN_MERGE(c, i) ((c)->ids[i][CIM_ID_BYTES - 1])

static bool read_source(const cim_config_t *c, const char *path, bool merge_file, size_t *used, size_t *count,
                        cim_result_t *result)
{
    FILE *file = fopen(path, "r");
    if (!file) return errno == ENOENT;
    bool ok = true;
    for (;;) {
        /* By byte: '\n' or NUL ends a line. */
        size_t length = 0;
        bool overflow = false, ended = false;
        int ch;
        while ((ch = fgetc(file)) != EOF) {
            if (ch == '\n' || ch == 0) {
                ended = true;
                break;
            }
            if (length + 1 < c->line_bytes) c->line[length++] = (char)ch;
            else overflow = true;
        }
        if (!length && !ended) break;
        if (!length) continue; /* an empty line, or a run of NUL */
        c->line[length] = '\0';
        if (overflow || !ended) {
            result->dropped++;
            if (ch == EOF) break;
            continue;
        }
        char id[CIM_ID_BYTES];
        memset(id, 0, sizeof(id));
        if (!c->command_id(c->context, c->line, id, sizeof(id)) || !id[0] || memchr(id, 0, CIM_ID_BYTES - 1) == NULL) {
            result->dropped++;
            continue;
        }
        size_t seen = *count;
        for (size_t i = 0; i < *count && seen == *count; ++i)
            if (!strcmp(c->ids[i], id)) seen = i;
        if (seen == *count) {
            if (*count >= c->max_ids || *used + length + 1 > c->capacity) {
                result->dropped++;
                continue;
            }
            memset(c->ids[*count], 0, CIM_ID_BYTES);
            memcpy(c->ids[*count], id, strlen(id));
            memcpy(c->merged + *used, c->line, length);
            c->merged[*used + length] = '\n';
            *used += length + 1;
            result->kept++;
            ++*count;
        }
        if (merge_file) CIM_IN_MERGE(c, seen) = 1;
    }
    /* An unreadable source is no source to remove after: stop here. */
    if (ferror(file)) ok = false;
    if (fclose(file) != 0) ok = false;
    return ok;
}

/* Appends to the merge file every kept line it does not hold yet, starting
 * on a line of its own, and puts it on flash. Never truncates it. */
static bool extend_merge_file(const cim_config_t *c, size_t used, size_t count)
{
    bool missing = false;
    for (size_t i = 0; i < count && !missing; ++i) missing = !CIM_IN_MERGE(c, i);
    if (!missing) return true;
    int last = '\n';
    FILE *file = fopen(c->merge, "r");
    if (file) {
        if (fseek(file, -1, SEEK_END) == 0) {
            int ch = fgetc(file);
            if (ch != EOF) last = ch;
        }
        if (fclose(file) != 0) return false;
    } else if (errno != ENOENT) {
        return false;
    }
    file = fopen(c->merge, "a");
    if (!file) return false;
    bool ok = last == '\n' || last == 0 || fwrite("\n", 1, 1, file) == 1;
    size_t start = 0;
    for (size_t i = 0; ok && i < count && start < used; ++i) {
        const char *end = memchr(c->merged + start, '\n', used - start);
        size_t length = end ? (size_t)(end - (c->merged + start)) + 1 : used - start;
        if (!CIM_IN_MERGE(c, i)) ok = fwrite(c->merged + start, 1, length, file) == length;
        start += length;
    }
    if (ok) ok = fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    return ok;
}

bool cim_merge(const cim_config_t *c, cim_result_t *result)
{
    if (!c || !result || !c->merged || !c->ids || !c->line || !c->command_id || !c->line_bytes) return false;
    memset(result, 0, sizeof(*result));
    const char *const sources[] = {c->active, c->backup, c->stage, c->merge};
    size_t used = 0, count = 0;
    for (size_t s = 0; s < sizeof(sources) / sizeof(sources[0]); ++s)
        if (!read_source(c, sources[s], s == 3, &used, &count, result)) return false;
    /* 1. Every kept line on flash in the merge file before anything goes. */
    if (!extend_merge_file(c, used, count)) return false;
    /* 2. A state ft_replace starts from, then the lines installed. */
    if (!ft_checkpoint_idle(c->port)) return false;
    if (remove(c->stage) != 0 && errno != ENOENT) return false;
    if (remove(c->backup) != 0 && errno != ENOENT) return false;
    FILE *stage = fopen(c->stage, "w");
    bool ok = stage && (!used || fwrite(c->merged, 1, used, stage) == used) && fflush(stage) == 0 &&
        fsync(fileno(stage)) == 0;
    if (stage && fclose(stage) != 0) ok = false;
    if (ok) ok = ft_replace(c->active, c->stage, c->backup, c->limit, c->port);
    /* 3. The merge file last: the installed inbox holds every line. */
    if (ok && remove(c->merge) != 0 && errno != ENOENT) ok = false;
    return ok;
}
