#pragma once
/* ZKT 2.7.5: the command inbox rebuilt from its readable lines after the
 * boot storage repair filled an unreadable span of it (NUL) or found a torn
 * last line. add_connector supplies the paths, its buffers and the test of a
 * line (it decrypts to a command with a command_id); the SPIFFS repair
 * harness runs the same code on its emulated flash through its stdio shim.
 *
 * Every line that names a command is kept once per command_id, from the
 * active file, its backup, its stage and a merge file an interrupted merge
 * left (in that order). A NUL ends a line like '\n' does: a fill that
 * swallowed a line's newline must not glue the intact line after it to the
 * damaged one (a line cut short does not decrypt and is dropped).
 *
 * Nothing is removed before every kept line is on flash in the merge file,
 * which is only ever appended to (an interrupted merge may already have
 * removed the other copies of what it holds); then the transaction is set
 * idle, the backup and stage go, the lines are staged and ft_replace installs
 * them, and the merge file goes last. A reset at any point leaves every line
 * in a file the next merge reads. */
#include <stdbool.h>
#include <stddef.h>

#include "file_transaction.h"

#define CIM_ID_BYTES 96U

typedef struct {
    const char *active, *backup, *stage, *merge;
    /* Caller's buffers: merged (capacity bytes), ids (max_ids slots of
     * CIM_ID_BYTES), line (line_bytes). */
    char *merged;
    size_t capacity;
    char (*ids)[CIM_ID_BYTES];
    size_t max_ids;
    char *line;
    size_t line_bytes;
    /* The line (no newline, NUL-terminated) is a command: its command_id
     * goes to id (fewer than CIM_ID_BYTES - 1 characters). */
    bool (*command_id)(void *context, const char *line, char *id, size_t id_bytes);
    void *context;
    ft_port_t port;
    size_t limit;          /* ft_replace's size limit for the installed inbox */
} cim_config_t;

typedef struct {
    unsigned kept, dropped;
} cim_result_t;

/* False when the merge did not finish: every source is still where it was,
 * or every line is in the merge file; the caller runs it again later. */
bool cim_merge(const cim_config_t *config, cim_result_t *result);
