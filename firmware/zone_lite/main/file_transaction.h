#pragma once
#include "durable_queue.h"
/* Replacement generations preserve the old file until the new prefix and the
 * committed generation are durable. Owners serialize all access to these paths. */
typedef struct {
    uint32_t version, generation, phase, length, digest, crc;
} ft_checkpoint_t;
typedef struct {
    int (*load)(void *, ft_checkpoint_t *);
    bool (*commit)(void *, const ft_checkpoint_t *);
    void *context;
} ft_port_t;
bool ft_recover(const char *active, const char *stage, const char *backup, ft_port_t port);
bool ft_replace(const char *active, const char *stage, const char *backup, size_t limit, ft_port_t port);

#define FT_PATH_BYTES 112U
#define FT_READ_SLICE_BYTES 4096U
typedef enum { FT_WORK_PENDING, FT_WORK_DONE, FT_WORK_FAILED } ft_work_result_t;
typedef struct {
    ft_checkpoint_t checkpoint;
    ft_port_t port;
    char paths[3][FT_PATH_BYTES];
    size_t limit;
    uint32_t scan_position, scan_digest;
    uint64_t bytes_read;
    uint8_t state, path_index, on_match, on_mismatch;
    bool replacement, computing_digest;
    int error;
    const char *operation;
} ft_work_t;

/* The storage owner must keep these three paths immutable to other producers
 * until completion/recovery. Each step reads at most FT_READ_SLICE_BYTES and
 * closes every handle before returning, so unrelated live work can run between
 * steps. A filesystem/NVS primitive can still block: this is a byte/work bound,
 * not a physical flash latency guarantee. Checkpoint format remains version 1;
 * existing ft_recover readers can recover any committed step after reboot. */
bool ft_work_begin(ft_work_t *work, const char *active, const char *stage, const char *backup,
                    size_t limit, ft_port_t port, bool replacement);
ft_work_result_t ft_work_step(ft_work_t *work);
